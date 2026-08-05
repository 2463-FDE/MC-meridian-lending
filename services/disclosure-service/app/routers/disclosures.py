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

**The provenance chain is read through the view, never re-joined here.** `GET
/disclosures/{id}/provenance` is one query on `v_disclosure_provenance` (spec D3: "pulls
from the KG" is a code-structure requirement, not just a schema one). Rebuilding the walk
with SQLAlchemy joins would put a second definition of the chain in application code, free
to drift from the one the auditor reads.

**The lifecycle is a whitelist, and `delivered` is terminal.** draft -> in_review ->
approved -> delivered, with a reject returning to draft and routing by reason code. The
DDL enforces the same shape one layer down (status CHECK, the delivered_at coupling, the
freeze trigger) so a direct SQL edit cannot do what this router refuses.

Internal-only, and idempotent per offer (uq_disclosures_offer), mirroring the offer write
path: a retry or a concurrent POST must not produce two regulated records for one offer.
"""

import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import apr as apr_mod
from .. import fingerprint, models, rules
from ..database import get_session
from ..logging_config import get_logger
from ..schemas import (
    DisclosureDocument,
    DisclosureIn,
    DisclosureOut,
    ProvenanceOut,
    TransitionIn,
    TransitionOut,
)
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
        # Repair the offer edge here, not only on the fresh-insert path. The edge write
        # used to happen after the disclosure was already committed, so a crash between
        # the two left it NULL — and every retry short-circuits on this line, so nothing
        # ever closed it again. The provenance view joins through
        # `offers.decision_event_id`, so the chain stayed incomplete for a disclosure that
        # exists and delivery stayed blocked short of hand-written SQL.
        #
        # The source is the persisted disclosure's own decision event, not the request
        # body's: that value was validated against the offer's application when the record
        # was written, and this path does not revalidate.
        if _close_provenance_edge(offer, existing.decision_event_id):
            session.commit()
        return _disclosure_out(existing)

    _refuse_if_decision_event_mismatched(session, body.decision_event_id, offer)

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

    # Checked against this service's own numbers before it can be stored, so the document
    # persisted next to a regulated figure can never spell that figure differently.
    _refuse_if_document_disagrees(body.document, outputs)

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
        # Deliberately NOT an input to the fingerprint above: that hash covers inputs +
        # ruleset + outputs and must recompute from the persisted snapshot alone (spec D3
        # acceptance 4). The document's figures are already pinned to those outputs by the
        # check above, so hashing the prose too would only make a regulated integrity value
        # depend on model wording.
        document_body=(
            body.document.model_dump() if body.document is not None else None
        ),
    )
    session.add(row)
    # Close the provenance edge inside the SAME transaction as the insert. `create_offer`
    # never writes `offers.decision_event_id`, so this is the only place it gets set; doing
    # it in a second commit meant the record could exist with its edge still open.
    _close_provenance_edge(offer, body.decision_event_id)
    try:
        session.commit()
    except IntegrityError:
        # Concurrent POST: the loser replays the winner's record rather than inserting a
        # second regulated document. Same posture as the offer write path. The rollback
        # discards this transaction's edge write too, so the loser has to close it again
        # from the winner's record — otherwise the winner's own crash window reopens here.
        session.rollback()
        winner = session.scalar(
            select(models.Disclosure).where(models.Disclosure.offer_id == body.offer_id)
        )
        if winner is None:
            raise
        if _close_provenance_edge(offer, winner.decision_event_id):
            session.commit()
        return _disclosure_out(winner)

    session.refresh(row)
    log.info(
        "disclosure persisted id=%s offer_id=%s fingerprint=%s",
        row.id,
        row.offer_id,
        row.content_fingerprint,
    )
    return _disclosure_out(row)


# The KG read. One statement, one object: the chain is defined once, in the view, and this
# is the only place application code walks it. Column list is explicit so a view reshape
# fails here loudly instead of silently dropping a field from the response.
_PROVENANCE_SQL = text(
    """
    SELECT disclosure_id, disclosure_status, disclosed_apr, compute_snapshot,
           fee_schedule_version, apr_method_version, content_fingerprint, delivered_at,
           offer_id, offer_apr, offer_created_at,
           decision_event_id, decision_outcome, policy_band, decided_at,
           application_id, applicant_id
    FROM v_disclosure_provenance
    WHERE disclosure_id = :disclosure_id
    """
)

# Every hop the chain must have to be whole. `applicant_id` is included because an
# application whose applicant row was deleted (the abandon path does exactly that) leaves a
# disclosure that cannot be traced to a person — a partial chain, not a complete one.
_CHAIN_EDGES = (
    "disclosure_id",
    "offer_id",
    "decision_event_id",
    "application_id",
    "applicant_id",
)


@router.get("/disclosures/{disclosure_id}/provenance", response_model=ProvenanceOut)
def read_provenance(
    disclosure_id: int,
    session: Session = Depends(get_session),
    x_internal_service: str | None = Header(default=None, alias="X-Internal-Service"),
):
    """Walk disclosure -> offer -> decision_event -> application -> applicant (spec D3.3).

    Internal-only for the same reason the write path is: the chain carries the disclosed
    APR and the applicant id, and `/disclosure/*` is reachable anonymously through the
    gateway. Origination's authorized routes are the only intended caller.
    """
    _require_internal_caller(x_internal_service)
    row = (
        session.execute(_PROVENANCE_SQL, {"disclosure_id": disclosure_id})
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="disclosure not found")
    return _provenance_out(row)


_PROVENANCE_BY_APPLICATION_SQL = text(
    """
    SELECT disclosure_id, disclosure_status, disclosed_apr, compute_snapshot,
           fee_schedule_version, apr_method_version, content_fingerprint, delivered_at,
           offer_id, offer_apr, offer_created_at,
           decision_event_id, decision_outcome, policy_band, decided_at,
           application_id, applicant_id
    FROM v_disclosure_provenance
    WHERE application_id = :application_id
    ORDER BY disclosure_id DESC NULLS LAST, offer_id DESC
    LIMIT 1
    """
)


@router.get(
    "/applications/{application_id}/disclosure/provenance",
    response_model=ProvenanceOut,
)
def read_provenance_by_application(
    application_id: int,
    session: Session = Depends(get_session),
    x_internal_service: str | None = Header(default=None, alias="X-Internal-Service"),
):
    """The same chain, entered from the application — what the officer's screen has.

    `uq_offers_app` and `uq_disclosures_offer` make at most one of each per application
    today, so the ORDER BY is a tiebreak against a future re-issue, not a policy: newest
    first, and a row with a disclosure beats one without.
    """
    _require_internal_caller(x_internal_service)
    row = (
        session.execute(
            _PROVENANCE_BY_APPLICATION_SQL, {"application_id": application_id}
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no offer for this application")
    return _provenance_out(row)


@router.get("/disclosures/{disclosure_id}/document", response_model=DisclosureDocument)
def read_document(
    disclosure_id: int,
    session: Session = Depends(get_session),
    x_internal_service: str | None = Header(default=None, alias="X-Internal-Service"),
):
    """The stored borrower-facing document, so a reviewer can read what they are approving.

    A separate route rather than a field on the provenance view, for two reasons. The view
    is the one definition of the CHAIN — edges and disclosed figures — and a document body
    is neither. And origination's `read_disclosure`, which proxies that view, admits the
    owning borrower and a continuation-token holder; putting the body there would hand a
    borrower the draft document, which is exactly what `generate_disclosure` was made
    officer-only to prevent. The officer-only wrapper is origination's
    `read_disclosure_document`.

    Internal-only, like every other route in this module: `/disclosure/*` is reachable
    anonymously through the gateway.
    """
    _require_internal_caller(x_internal_service)
    row = session.get(models.Disclosure, disclosure_id)
    if row is None:
        raise HTTPException(status_code=404, detail="disclosure not found")
    if row.document_body is None:
        # 404, not 204: rows written before migration 0012 legitimately have none, and the
        # officer's screen needs to tell "nothing recorded" apart from "recorded and empty".
        raise HTTPException(
            status_code=404, detail="no document recorded for this disclosure"
        )
    return row.document_body


def _provenance_out(row) -> ProvenanceOut:
    missing = [edge for edge in _CHAIN_EDGES if row.get(edge) is None]
    return ProvenanceOut(
        disclosure_id=row.get("disclosure_id"),
        disclosure_status=row.get("disclosure_status"),
        # Numeric -> string for the same reason DisclosureOut does it: a JSON float would
        # reintroduce the representation problem the NUMERIC column exists to avoid.
        disclosed_apr=_text_or_none(row.get("disclosed_apr")),
        compute_snapshot=row.get("compute_snapshot"),
        fee_schedule_version=row.get("fee_schedule_version"),
        apr_method_version=row.get("apr_method_version"),
        content_fingerprint=row.get("content_fingerprint"),
        delivered_at=_text_or_none(row.get("delivered_at")),
        offer_id=row.get("offer_id"),
        offer_apr=row.get("offer_apr"),
        offer_created_at=_text_or_none(row.get("offer_created_at")),
        decision_event_id=row.get("decision_event_id"),
        decision_outcome=row.get("decision_outcome"),
        policy_band=row.get("policy_band"),
        decided_at=_text_or_none(row.get("decided_at")),
        application_id=row.get("application_id"),
        applicant_id=row.get("applicant_id"),
        chain_complete=not missing,
        missing_edges=missing,
    )


def _text_or_none(value):
    """Timestamps and Numerics leave psycopg2 as datetime/Decimal; both serialize as ISO
    text here rather than through Pydantic's float/date coercion."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# Lifecycle (spec D6). The DDL already carries the status vocabulary, the delivered_at
# coupling, and the freeze-on-delivery trigger; this is the machine that drives them.
# ---------------------------------------------------------------------------

# Whitelist, not a blacklist: an unlisted pair is illegal. `delivered` has no outgoing
# edge at all — the borrower has seen the document, and the database trigger enforces the
# same thing one layer down if this check is ever bypassed.
LEGAL_TRANSITIONS = {
    "draft": {"in_review"},
    "in_review": {"approved", "draft"},
    "approved": {"delivered", "draft"},
    "delivered": set(),
}

# A reject is a return to `draft` — the document is not deliverable and the work goes
# back somewhere. WHERE it goes is the reason code's job (spec D5, stage 6).
REJECT_ROUTES = {
    # Presentational: the numbers stand, the maker re-renders.
    "wording": "assemble",
    "formatting": "assemble",
    # Substantive: re-rendering cannot fix a wrong loan. Terminal exit to decisioning.
    "wrong_terms": "decisioning",
    "wrong_rate": "decisioning",
    "ineligible": "decisioning",
}

# Reg Z 1026.17(b): the disclosure is made BEFORE consummation. A boarded loan is this
# system's consummation event, so delivering afterwards is a timing violation, not a
# late notification.
_CONSUMMATED_SQL = text(
    """
    SELECT 1 FROM loans l
    JOIN offers o ON o.app_id = l.app_id
    WHERE o.id = :offer_id
    LIMIT 1
    """
)


@router.post("/disclosures/{disclosure_id}/transition", response_model=TransitionOut)
def transition_disclosure(
    disclosure_id: int,
    body: TransitionIn,
    session: Session = Depends(get_session),
    x_internal_service: str | None = Header(default=None, alias="X-Internal-Service"),
):
    """Drive draft -> in_review -> approved -> delivered (spec D6).

    Three things are refused rather than accommodated: an illegal transition, a reject
    with no routable reason code, and a delivery after the loan has already been boarded.
    """
    _require_internal_caller(x_internal_service)

    row = session.get(models.Disclosure, disclosure_id)
    if row is None:
        raise HTTPException(status_code=404, detail="disclosure not found")

    current = row.status
    target = body.to_status
    if target not in LEGAL_TRANSITIONS.get(current, set()):
        raise HTTPException(
            status_code=409,
            detail=f"illegal transition {current} -> {target}",
        )

    routed_to = None
    if target == "draft":
        routed_to = REJECT_ROUTES.get(body.reason_code or "")
        if routed_to is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "a reject needs a routable reason_code, one of "
                    f"{', '.join(sorted(REJECT_ROUTES))}"
                ),
            )

    values = {"status": target}
    if target == "delivered":
        _refuse_if_no_document(row)
        _refuse_if_chain_incomplete(session, disclosure_id)
        _refuse_if_already_consummated(session, row.offer_id)
        # The only write of delivered_at, and the only status that sets it. The DDL check
        # constraint asserts the same coupling from the other side.
        values["delivered_at"] = datetime.now(timezone.utc)

    # Guarded on the status we read: two officers acting at once must not both win. The
    # loser gets a 409 rather than a trigger exception from an UPDATE on a row that moved
    # underneath it.
    result = session.execute(
        update(models.Disclosure)
        .where(
            models.Disclosure.id == disclosure_id,
            models.Disclosure.status == current,
        )
        .values(**values)
    )
    if result.rowcount == 0:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="disclosure changed status concurrently; re-read it"
        )
    # The session expires on commit, so reading `row` below re-reads what was written
    # rather than reporting the pre-transition state back to the caller.
    session.commit()

    log.info(
        "disclosure transition id=%s %s -> %s reason=%s routed_to=%s",
        disclosure_id,
        current,
        target,
        body.reason_code or "",
        routed_to or "",
    )
    return TransitionOut(**_disclosure_out(row).model_dump(), routed_to=routed_to)


def _refuse_if_no_document(row: models.Disclosure) -> None:
    """Delivery is the delivery OF SOMETHING, so there has to be a something.

    Before this check the transition wrote `status` and `delivered_at` and asserted nothing
    about the document those two claim was sent. The assembled borrower-facing document
    existed only in the generating call's HTTP response, so the compliance reviewer who
    approved and delivered it — a different person and a different session under
    maker-checker — could not read it at all, and `accept_offer` then treats the row as
    boardable. That made `delivered` a flag over content no human had seen.

    Checked first among the three delivery guards: it is a property of the row already in
    hand, so it costs no query, and "there is no document" is the more basic refusal than
    "the document's provenance is incomplete".

    Refused rather than backfilled. A disclosure whose document was never recorded cannot
    have one invented at delivery time without fabricating the evidence; regenerating it
    means a new run through the pipeline, where the figure gate applies.
    """
    if row.document_body is None:
        log.error("refusing delivery of disclosure_id=%s: no document recorded", row.id)
        raise HTTPException(
            status_code=409,
            detail=(
                "no document is recorded for this disclosure; refusing to deliver a "
                "disclosure whose borrower-facing document was never persisted"
            ),
        )


def _refuse_if_chain_incomplete(session: Session, disclosure_id: int) -> None:
    """Delivery is the irreversible step, so the chain is checked here too.

    The pipeline already gates on this at stage 5, but the lifecycle is reachable without
    it — a disclosure whose offer carries no `app_id` walks to a NULL application and
    applicant. Leaving the check only in the pipeline (and in the UI, which disables the
    button) would mean the API enforces less than the screen does, and the row that
    escapes is frozen the moment it is written.
    """
    row = (
        session.execute(_PROVENANCE_SQL, {"disclosure_id": disclosure_id})
        .mappings()
        .first()
    )
    missing = (
        [edge for edge in _CHAIN_EDGES if row.get(edge) is None]
        if row is not None
        else list(_CHAIN_EDGES)
    )
    if missing:
        log.error(
            "refusing delivery of disclosure_id=%s: incomplete chain missing=%s",
            disclosure_id,
            ",".join(missing),
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "provenance chain is incomplete ("
                f"{', '.join(missing)}); refusing to deliver"
            ),
        )


def _refuse_if_already_consummated(session: Session, offer_id: int) -> None:
    consummated = session.execute(_CONSUMMATED_SQL, {"offer_id": offer_id}).first()
    if consummated:
        log.error("refusing delivery after consummation offer_id=%s", offer_id)
        raise HTTPException(
            status_code=409,
            detail=(
                "TILA timing: the loan is already boarded; the disclosure had to be "
                "delivered before consummation"
            ),
        )


def _close_provenance_edge(offer: models.Offer, decision_event_id: int | None) -> bool:
    """Point the offer at its decision event when that edge is still open.

    Idempotent by construction: an offer that already carries an edge is left alone, so
    every path through this endpoint can call it. Returns whether it changed anything, so
    the replay paths know whether a commit is owed.
    """
    if offer.decision_event_id is not None or decision_event_id is None:
        return False
    offer.decision_event_id = decision_event_id
    return True


_DECISION_EVENT_APP_SQL = text(
    "SELECT app_id FROM decision_events WHERE id = :decision_event_id"
)


def _refuse_if_decision_event_mismatched(
    session: Session, decision_event_id: int, offer: models.Offer
) -> None:
    """The FK proves `decision_event_id` exists; it does not prove it is THIS offer's.

    Without this, any internal caller supplying a valid-but-wrong-applicant decision
    event mints a disclosure whose provenance view reports `chain_complete: true` —
    indistinguishable from a correct chain, and worse than the partial-chain case the
    view already handles, because it is silent rather than flagged.
    """
    row = session.execute(
        _DECISION_EVENT_APP_SQL, {"decision_event_id": decision_event_id}
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="decision event not found")
    if row[0] != offer.app_id:
        log.error(
            "refusing disclosure for offer_id=%s: decision_event_id=%s belongs to "
            "app_id=%s, not this offer's app_id=%s",
            offer.id,
            decision_event_id,
            row[0],
            offer.app_id,
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "decision_event_id does not belong to this offer's application; "
                "refusing to persist"
            ),
        )


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


# document figure name -> the minor-unit output it must spell. `apr` is handled separately:
# it is exact NUMERIC, not cents.
_DOCUMENT_MONEY_FIELDS = {
    "finance_charge": "finance_charge_cents",
    "amount_financed": "amount_financed_cents",
    "monthly_payment": "monthly_payment_cents",
    "total_of_payments": "total_of_payments_cents",
}


# A figure as a borrower may be shown it: digits, optionally a decimal point and more
# digits. Nothing else. `Decimal` is far more permissive than that — it accepts underscore
# separators, a leading sign, and scientific notation, so `17_460.00`, `+3628.71` and
# `3.62871E+3` all parse to values that COMPARE EQUAL to the record. This string is stored
# verbatim and printed verbatim to the officer reviewing it, so a numeric-only check would
# admit a disclosure reading "Amount Financed $3.62871E+3". Reject the spelling as well as
# the value. Unsigned by design: the DDL's amount checks make a negative disclosed figure
# impossible, so a sign here is malformed rather than negative.
_PLAIN_DECIMAL = re.compile(r"^\d+(\.\d+)?$")


def _parse_figure(value: str) -> Decimal | None:
    """A rendered figure back to an exact Decimal, or None if it is not a plain one.

    None covers a non-numeric string, a spelling `Decimal` would accept but a borrower must
    never be shown (see `_PLAIN_DECIMAL`), and NaN / Infinity — the latter parse and then
    compare unequal to everything, which would read as an ordinary mismatch rather than the
    malformed document it is.
    """
    candidate = value.strip()
    if not _PLAIN_DECIMAL.match(candidate):
        return None
    try:
        parsed = Decimal(candidate)
    except ArithmeticError:
        return None
    return parsed if parsed.is_finite() else None


def _refuse_if_document_disagrees(
    document: DisclosureDocument | None, outputs: dict
) -> None:
    """Fail closed when the document spells a figure differently from the record.

    The upstream pipeline already compares rendered figures against computed ones at its
    verify stage, but that check runs in the caller. This is the authoritative boundary, and
    the whole point of storing the document with the row is that a reviewer can trust the
    two agree — so the boundary proves it rather than trusting the caller to have.

    Compared as exact Decimals, not as strings: `9.584` and `9.5840` are one number with two
    spellings and must not read as a mismatch, while `9.58` against `9.584` must. Money is
    compared against cents-over-100 so a sub-cent spelling (`3628.706`) is refused rather
    than quietly rounded into agreement.
    """
    if document is None:
        return

    mismatched = []
    for field, cents_key in _DOCUMENT_MONEY_FIELDS.items():
        rendered = _parse_figure(getattr(document.figures, field))
        if rendered is None or rendered != Decimal(outputs[cents_key]) / 100:
            mismatched.append(field)
    rendered_apr = _parse_figure(document.figures.apr)
    if rendered_apr is None or rendered_apr != outputs["apr"]:
        mismatched.append("apr")

    if mismatched:
        log.error(
            "refusing disclosure document: figures disagree on %s",
            ",".join(sorted(mismatched)),
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "the document's figures disagree with the recomputed disclosure on "
                f"{', '.join(sorted(mismatched))}; refusing to persist"
            ),
        )
