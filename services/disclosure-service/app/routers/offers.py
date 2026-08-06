"""Offer / Truth-in-Lending disclosure generation (disclosure-service).

Write path (POST /offers) builds the offer + amortization schedule with float math and
persists an offers row via raw psycopg2 (matches the LOS write path). Read path
(GET /applications/{id}/offer) goes through SQLAlchemy.
"""

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from psycopg2 import errors as pg_errors
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config, db, models, offer as offer_mod, rules, schedule
from ..database import get_session
from ..logging_config import get_logger
from ..schemas import Disclosure, OfferIn, OfferResponse, ScheduleRow

log = get_logger("offers")
router = APIRouter(tags=["offers"])


def _require_internal_caller(x_internal_service: str | None) -> None:
    """Gate a route to internal service-to-service callers (PR review).

    Mirrors the decision/origination/kyc guards: the gateway strips any client-supplied
    X-Internal-Service, so only a caller reaching this service directly with the shared
    secret is accepted. Fails closed when the token is unconfigured (503); constant-time
    byte compare so the token cannot be timed out or crash on a non-ASCII value.
    """
    expected = config.INTERNAL_SERVICE_TOKEN
    if not expected:
        log.error("INTERNAL_SERVICE_TOKEN not configured; refusing internal route")
        raise HTTPException(status_code=503, detail="internal auth not configured")
    if not x_internal_service or not hmac.compare_digest(
        x_internal_service.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=403, detail="internal service identity required"
        )


# The note rate the LOS applies server-side. Only the fallback for a legacy offer with no
# disclosure record — a real one carries its own rate in compute_snapshot.
_FALLBACK_NOTE_RATE_PCT = 7.99


def _schedule_inputs(row, amount_financed: float) -> tuple:
    """Principal and NOTE rate for the display schedule.

    Prefers the disclosure's `compute_snapshot`: those are the values the disclosed figures
    were actually derived from, so the schedule and the TILA box cannot disagree. Falls back
    to inverting amount_financed with the current fee rate only for offers that predate the
    disclosures table.
    """
    offer_id = row.get("id") if hasattr(row, "get") else row["id"]
    if offer_id is not None:
        snapshots = db.query(
            "SELECT compute_snapshot FROM disclosures WHERE offer_id = %s", (offer_id,)
        )
        snapshot = snapshots[0].get("compute_snapshot") if snapshots else None
        if snapshot:
            try:
                return (
                    int(snapshot["principal_cents"]) / 100,
                    float(snapshot["note_rate_pct"]),
                )
            except (KeyError, TypeError, ValueError):
                # A malformed snapshot falls through to the inversion rather than raising:
                # the schedule is a display aid, and refusing to render the offer at all
                # would be a worse answer than rendering it the legacy way.
                log.warning("unusable compute_snapshot for offer_id=%s", offer_id)
    principal = (
        round(amount_financed / (1 - rules.get_fee_schedule().origination_fee_pct), 2)
        if amount_financed
        else 0.0
    )
    return principal, _FALLBACK_NOTE_RATE_PCT


_PERSISTED_OFFER_COLUMNS = (
    "id, apr, finance_charge, monthly_payment, amount_financed, total_of_payments, "
    "decision_event_id"
)


def _require_decision_event_for_application(
    decision_event_id: int, application_id: int
) -> None:
    """The FK proves the event exists; it does not prove it is THIS application's.

    Same posture as `create_disclosure`'s guard one table down. Writing a foreign
    application's event onto the offer would make `v_disclosure_provenance` — which joins
    the decision through `offers.decision_event_id` — report a whole chain to the wrong
    applicant.
    """
    rows = db.query(
        "SELECT app_id FROM decision_events WHERE id = %s", (decision_event_id,)
    )
    if not rows:
        raise HTTPException(status_code=404, detail="decision event not found")
    if rows[0]["app_id"] != application_id:
        log.error(
            "refusing offer for application_id=%s: decision_event_id=%s belongs to "
            "app_id=%s",
            application_id,
            decision_event_id,
            rows[0]["app_id"],
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "decision_event_id does not belong to this application; "
                "refusing to persist"
            ),
        )


def _offer_response_from_persisted(row, application_id: int) -> OfferResponse:
    """Build an OfferResponse from a PERSISTED offers row (the create_offer replay paths and
    the GET read path). The disclosure numbers come straight from the stored row -- never
    recomputed from a request body -- so a retry with drifted inputs returns the offer that was
    actually persisted (and that accept_offer will board), not a fresh divergent one. The
    display schedule is reconstructed from the stored disclosure box the same way the LOS read
    path does: back out principal from amount_financed via the origination fee, recover term
    from total/monthly, and reuse the stored APR as the schedule rate. Float math throughout
    (D1); the origination fee comes from the one versioned schedule (rules.py).

    The schedule rate is the NOTE rate, never the APR. The APR carries the origination fee
    and is therefore higher than the rate interest actually accrues at, so amortizing at it
    produces a schedule that contradicts the disclosed figures in the same response — on
    15000/7.99/36 the rows summed to 17442.72 against a disclosed total of payments of
    16919.15, and showed a 484.52 payment against a disclosed 469.98. The fresh-insert path
    below always passed the note rate; only this replay/read path did not, so an offer showed
    one schedule when created and a different one when read back, which is what the portal
    displays. Found by the teeth pass, reproduced live.

    `disclosures.compute_snapshot` is the source for principal and note rate when a
    disclosure exists — the values actually used, recorded at compute time. That closes the
    inference this docstring used to flag: backing principal out of a STORED amount_financed
    with the CURRENT fee rate is only correct while the rate has not moved. Legacy offers
    (no disclosure row) still take the inversion, because for them there is nothing better.
    """
    apr = row["apr"] or 0
    finance_charge = row["finance_charge"] or 0
    monthly_payment = row["monthly_payment"] or 0.0
    total_of_payments = row["total_of_payments"] or 0.0
    amount_financed = row["amount_financed"] or 0.0
    term_months = round(total_of_payments / monthly_payment) if monthly_payment else 0
    principal, note_rate_pct = _schedule_inputs(row, amount_financed)
    rows = (
        schedule.amortization(principal, note_rate_pct, term_months)
        if term_months
        else []
    )
    disclosure = Disclosure(
        apr=apr,
        finance_charge=finance_charge,
        monthly_payment=monthly_payment,
        amount_financed=amount_financed,
        total_of_payments=total_of_payments,
    )
    return OfferResponse(
        offer_id=row["id"],
        application_id=application_id,
        apr=apr,
        finance_charge=finance_charge,
        monthly_payment=monthly_payment,
        total_of_payments=total_of_payments,
        disclosure=disclosure,
        schedule=[ScheduleRow(**r) for r in rows],
        # Echoed from the STORED row, never from the request: a replay is answered with the
        # event that authorized the persisted offer, which is what the disclosure must cite.
        # `.get`, because a legacy offer row predates the column being selected at all.
        decision_event_id=row.get("decision_event_id"),
    )


@router.post("/offers", response_model=OfferResponse)
def create_offer(
    body: OfferIn,
    x_internal_service: str | None = Header(default=None, alias="X-Internal-Service"),
):
    # Internal-only (PR review): this persists a TILA/Reg-Z offer (offers row) from
    # caller-supplied inputs and is reachable through the gateway's anonymous /disclosure
    # proxy. Without this an external caller could write a fabricated disclosure for any
    # app id. Only origination calls it (offer flow), forwarding the shared secret; the
    # gateway strips any client-supplied copy.
    _require_internal_caller(x_internal_service)
    # Idempotent per application (PR review): a double-click / browser retry / gateway-timeout
    # replay must not persist a SECOND regulated TILA disclosure. Reuse the existing offer when
    # one is already recorded, and REPLAY IT FROM THE PERSISTED ROW -- never from freshly
    # computed terms. Origination sends POLICY_RATE_PCT (and the stored app amount/term) on
    # every POST, so a retry after a policy-rate deploy or a term correction carries drifted
    # inputs; returning those under the old offer_id would disclose an APR/payment the borrower
    # cannot actually accept, because accept_offer boards from the stored offer row. The
    # uq_offers_app unique index (migration 0010) is the AUTHORITATIVE guard for the concurrent
    # race: the loser catches UniqueViolation and replays the winner's persisted offer. Mirrors
    # accept_offer's idempotent loan boarding (origination).
    existing = db.query(
        f"SELECT {_PERSISTED_OFFER_COLUMNS} FROM offers WHERE app_id = %s "
        "ORDER BY id LIMIT 1",
        (body.application_id,),
    )
    if existing:
        return _offer_response_from_persisted(existing[0], body.application_id)
    # The authorizing decision is recorded WITH the offer. Deferring it to disclosure time
    # meant the edge was closed from whichever decision event was latest by then, so a
    # re-decision between the offer and its disclosure silently re-parented the offer to an
    # event that did not authorize it — and `v_disclosure_provenance` then reported that
    # chain as complete. `uq_offers_app` makes the offer permanent, so the moment it is
    # written is the only moment its authorizing event is known rather than inferred.
    if body.decision_event_id is not None:
        _require_decision_event_for_application(
            body.decision_event_id, body.application_id
        )
    o = offer_mod.build_offer(body.principal, body.annual_rate, body.term_months)
    rows = schedule.amortization(body.principal, body.annual_rate, body.term_months)
    # persist via raw psycopg2 (matches origination's write path) — float money columns
    try:
        inserted = db.query(
            "INSERT INTO offers (app_id, apr, finance_charge, monthly_payment, "
            "amount_financed, total_of_payments, decision_event_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id",
            (
                body.application_id,
                o["apr"],
                o["finance_charge"],
                o["monthly_payment"],
                o["amount_financed"],
                o["total_of_payments"],
                body.decision_event_id,
            ),
        )
        offer_id = inserted[0]["id"]
    except pg_errors.UniqueViolation:
        # A concurrent create won the race and inserted first; replay its persisted offer
        # instead of a second one (one offer per app_id, enforced by uq_offers_app).
        won = db.query(
            f"SELECT {_PERSISTED_OFFER_COLUMNS} FROM offers WHERE app_id = %s "
            "ORDER BY id LIMIT 1",
            (body.application_id,),
        )
        if not won:
            raise HTTPException(
                status_code=409,
                detail="offer conflict without a retrievable offer",
            )
        return _offer_response_from_persisted(won[0], body.application_id)
    # Fresh insert: return the exact computed offer + true amortization schedule (equal to what
    # was just persisted; the replay paths above reconstruct from the stored row).
    disclosure = Disclosure(
        apr=o["apr"],
        finance_charge=o["finance_charge"],
        monthly_payment=o["monthly_payment"],
        amount_financed=o["amount_financed"],
        total_of_payments=o["total_of_payments"],
    )
    return OfferResponse(
        offer_id=offer_id,
        application_id=body.application_id,
        apr=o["apr"],
        finance_charge=o["finance_charge"],
        monthly_payment=o["monthly_payment"],
        total_of_payments=o["total_of_payments"],
        disclosure=disclosure,
        schedule=[ScheduleRow(**r) for r in rows],
        decision_event_id=body.decision_event_id,
    )


@router.get("/applications/{application_id}/offer", response_model=OfferResponse)
def get_offer(
    application_id: int,
    session: Session = Depends(get_session),
    x_internal_service: str | None = Header(default=None, alias="X-Internal-Service"),
):
    # Internal-only (PR review): this read discloses APR/finance charge/payment/schedule
    # for an enumerable app id and is reachable through the gateway's anonymous /disclosure
    # proxy. Without this an external caller could enumerate persisted TILA offers for any
    # app id, bypassing the origination /los/applications/{id}/offer owner/officer/token
    # gate. Only origination calls it (offer read), forwarding the shared secret; the
    # gateway strips any client-supplied copy.
    _require_internal_caller(x_internal_service)
    offer = session.scalar(
        select(models.Offer)
        .where(models.Offer.app_id == application_id)
        .order_by(models.Offer.id.desc())
    )
    if not offer:
        raise HTTPException(status_code=404, detail="no offer for this application")
    # Build the response from the persisted offer via the shared reconstruction used by the
    # create_offer replay paths -- one place for the stored-row -> disclosure math, so the two
    # cannot drift apart (PR review).
    return _offer_response_from_persisted(
        {
            "id": offer.id,
            "apr": offer.apr,
            "finance_charge": offer.finance_charge,
            "monthly_payment": offer.monthly_payment,
            "amount_financed": offer.amount_financed,
            "total_of_payments": offer.total_of_payments,
            "decision_event_id": offer.decision_event_id,
        },
        application_id,
    )
