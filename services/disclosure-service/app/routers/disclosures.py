"""Disclosure persistence — the authoritative TILA record (ADR 0012, spec D3/D5).

Two design choices carry most of the weight here.

**The endpoint recomputes; it never accepts numbers.** A caller supplies the loan's
inputs (offer, decision event, principal, rate, term) and this service derives every
disclosed figure in Decimal. Nothing upstream — including any agent — can hand it an APR
or a finance charge. That is the ADR 0012 invariant enforced at the boundary rather than
trusted at the call site.

**The recomputation is checked against the persisted offer.** If the inputs do not
reproduce the numbers already stored on the offer row, the request is REFUSED rather than
persisted. Silently writing a disclosure that disagrees with the offer the borrower was
shown is the exact failure this week exists to close, and it fails closed.

Internal-only, and idempotent per offer (uq_disclosures_offer), mirroring the offer write
path: a retry or a concurrent POST must not produce two regulated records for one offer.
"""

from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import apr as apr_mod
from .. import fingerprint, models, rules
from ..database import get_session
from ..logging_config import get_logger
from ..schemas import DisclosureIn, DisclosureOut
from .offers import _require_internal_caller

log = get_logger("disclosure")
router = APIRouter(tags=["disclosures"])

# Bumped when the compute method changes, so a stored row says which method produced it.
# Absence of this value on a legacy record means the pre-ADR-0012 add-on method.
APR_METHOD_VERSION = "actuarial-regz-appj-1"

CENTS = Decimal("0.01")
# The offer columns are float and were rounded to cents on write, so the comparison is to
# the cent, not exact. A wider band would let a genuinely different loan through.
OFFER_MATCH_TOLERANCE = Decimal("0.01")


def _to_cents(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _disclosure_out(row: models.Disclosure) -> DisclosureOut:
    return DisclosureOut(
        disclosure_id=row.id,
        offer_id=row.offer_id,
        decision_event_id=row.decision_event_id,
        status=row.status,
        apr=str(row.apr),
        finance_charge_cents=row.finance_charge_cents,
        amount_financed_cents=row.amount_financed_cents,
        monthly_payment_cents=row.monthly_payment_cents,
        total_of_payments_cents=row.total_of_payments_cents,
        fee_schedule_version=row.fee_schedule_version,
        apr_method_version=row.apr_method_version,
        content_fingerprint=row.content_fingerprint,
        delivered_at=row.delivered_at.isoformat() if row.delivered_at else None,
    )


@router.post("/disclosures", response_model=DisclosureOut, status_code=201)
def create_disclosure(
    body: DisclosureIn,
    session: Session = Depends(get_session),
    x_internal_service: str | None = Header(default=None, alias="X-Internal-Service"),
):
    _require_internal_caller(x_internal_service)

    offer = session.get(models.Offer, body.offer_id)
    if offer is None:
        raise HTTPException(status_code=404, detail="offer not found")

    # Idempotent replay before any compute: a retry returns the persisted record, never a
    # freshly derived one, so a rules change between attempts cannot swap the document.
    existing = session.scalar(
        select(models.Disclosure).where(models.Disclosure.offer_id == body.offer_id)
    )
    if existing is not None:
        return _disclosure_out(existing)

    schedule = rules.get_fee_schedule()
    payment = apr_mod.monthly_payment(
        body.principal, body.annual_rate, body.term_months
    )
    computed = {
        "apr": apr_mod.compute_apr(body.principal, body.annual_rate, body.term_months),
        "finance_charge": apr_mod.finance_charge(
            body.principal, body.annual_rate, body.term_months
        ),
        "amount_financed": apr_mod.amount_financed(body.principal),
        "monthly_payment": payment.quantize(CENTS, rounding=ROUND_HALF_UP),
        "total_of_payments": (payment * body.term_months).quantize(
            CENTS, rounding=ROUND_HALF_UP
        ),
    }

    _refuse_if_offer_disagrees(offer, computed)

    snapshot = {
        # Identifier-free (ADR 0007): loan terms only, no applicant attributes.
        "principal_cents": _to_cents(Decimal(str(body.principal))),
        "note_rate_pct": str(Decimal(str(body.annual_rate))),
        "term_months": body.term_months,
        "fee_pct": str(Decimal(str(schedule.origination_fee_pct))),
    }
    outputs = {
        "apr": computed["apr"],
        "finance_charge_cents": _to_cents(computed["finance_charge"]),
        "amount_financed_cents": _to_cents(computed["amount_financed"]),
        "monthly_payment_cents": _to_cents(computed["monthly_payment"]),
        "total_of_payments_cents": _to_cents(computed["total_of_payments"]),
    }

    row = models.Disclosure(
        offer_id=body.offer_id,
        decision_event_id=body.decision_event_id,
        status="draft",
        apr=computed["apr"],
        finance_charge_cents=outputs["finance_charge_cents"],
        amount_financed_cents=outputs["amount_financed_cents"],
        monthly_payment_cents=outputs["monthly_payment_cents"],
        total_of_payments_cents=outputs["total_of_payments_cents"],
        compute_snapshot=snapshot,
        fee_schedule_version=schedule.version,
        apr_method_version=APR_METHOD_VERSION,
        content_fingerprint=fingerprint.compute_fingerprint(
            inputs=snapshot,
            fee_schedule_version=schedule.version,
            apr_method_version=APR_METHOD_VERSION,
            outputs=outputs,
        ),
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # Concurrent POST: the loser replays the winner's record rather than inserting a
        # second regulated document. Same posture as the offer write path.
        session.rollback()
        winner = session.scalar(
            select(models.Disclosure).where(models.Disclosure.offer_id == body.offer_id)
        )
        if winner is None:
            raise
        return _disclosure_out(winner)

    session.refresh(row)
    # Close the provenance edge on the offer if it is still open (legacy rows, and offers
    # created before the coordinator started supplying it).
    if offer.decision_event_id is None:
        offer.decision_event_id = body.decision_event_id
        session.commit()
    log.info(
        "disclosure persisted id=%s offer_id=%s fingerprint=%s",
        row.id,
        row.offer_id,
        row.content_fingerprint,
    )
    return _disclosure_out(row)


def _refuse_if_offer_disagrees(offer: models.Offer, computed: dict) -> None:
    """Fail closed when the supplied inputs do not reproduce the stored offer.

    Without this, a caller passing a different principal or term would silently mint a
    disclosure for a loan the borrower was never offered — and it would look authoritative,
    because it is the minor-unit record.
    """
    checks = (
        ("apr", offer.apr, computed["apr"]),
        ("finance_charge", offer.finance_charge, computed["finance_charge"]),
        ("monthly_payment", offer.monthly_payment, computed["monthly_payment"]),
        ("amount_financed", offer.amount_financed, computed["amount_financed"]),
        ("total_of_payments", offer.total_of_payments, computed["total_of_payments"]),
    )
    mismatches = [
        name
        for name, stored, derived in checks
        if stored is None or abs(Decimal(str(stored)) - derived) > OFFER_MATCH_TOLERANCE
    ]
    if mismatches:
        log.error(
            "refusing disclosure for offer_id=%s: recomputed values disagree on %s",
            offer.id,
            ",".join(mismatches),
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "recomputed disclosure disagrees with the persisted offer on "
                f"{', '.join(mismatches)}; refusing to persist"
            ),
        )
