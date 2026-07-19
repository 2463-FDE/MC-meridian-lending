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

from .. import config, db, models, offer as offer_mod, schedule
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
    o = offer_mod.build_offer(body.principal, body.annual_rate, body.term_months)
    rows = schedule.amortization(body.principal, body.annual_rate, body.term_months)
    # Idempotent per application (PR review): a double-click / browser retry / gateway-
    # timeout replay must not persist a SECOND regulated TILA disclosure. Reuse the existing
    # offer id when one is already recorded -- the offer is deterministic from the server-
    # derived inputs (origination binds principal/term from the stored application + a policy
    # rate), so the disclosure rebuilt below equals the stored one. The uq_offers_app unique
    # index (migration 0010) is the AUTHORITATIVE guard for the concurrent race: the loser
    # catches UniqueViolation and replays the winner's offer instead of inserting a second.
    # Mirrors accept_offer's idempotent loan boarding (origination).
    existing = db.query(
        "SELECT id FROM offers WHERE app_id = %s ORDER BY id LIMIT 1",
        (body.application_id,),
    )
    if existing:
        offer_id = existing[0]["id"]
    else:
        # persist via raw psycopg2 (matches origination's write path) — float money columns
        try:
            inserted = db.query(
                "INSERT INTO offers (app_id, apr, finance_charge, monthly_payment, "
                "amount_financed, total_of_payments) VALUES (%s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (
                    body.application_id,
                    o["apr"],
                    o["finance_charge"],
                    o["monthly_payment"],
                    o["amount_financed"],
                    o["total_of_payments"],
                ),
            )
            offer_id = inserted[0]["id"]
        except pg_errors.UniqueViolation:
            # A concurrent create won the race and inserted first; serve its offer instead
            # of a second one (one offer per app_id, enforced by uq_offers_app).
            won = db.query(
                "SELECT id FROM offers WHERE app_id = %s ORDER BY id LIMIT 1",
                (body.application_id,),
            )
            if not won:
                raise HTTPException(
                    status_code=409,
                    detail="offer conflict without a retrievable offer",
                )
            offer_id = won[0]["id"]
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
    # Rebuild the display schedule from the persisted offer (Offer ORM only). Recover the
    # principal/term from the stored disclosure box and reuse the stored APR as the schedule
    # rate — the same shortcut the LOS read path takes. Float math throughout (D1); the
    # third drifted fee copy (offer.ORIGINATION_FEE_PCT = 0.03) is used to back out principal.
    monthly_payment = offer.monthly_payment or 0.0
    total_of_payments = offer.total_of_payments or 0.0
    amount_financed = offer.amount_financed or 0.0
    principal = (
        round(amount_financed / (1 - offer_mod.ORIGINATION_FEE_PCT), 2)
        if amount_financed
        else 0.0
    )
    term_months = round(total_of_payments / monthly_payment) if monthly_payment else 0
    rows = (
        schedule.amortization(principal, offer.apr or 7.99, term_months)
        if term_months
        else []
    )
    disclosure = Disclosure(
        apr=offer.apr or 0,
        finance_charge=offer.finance_charge or 0,
        monthly_payment=monthly_payment,
        amount_financed=amount_financed,
        total_of_payments=total_of_payments,
    )
    return OfferResponse(
        offer_id=offer.id,
        application_id=application_id,
        apr=offer.apr or 0,
        finance_charge=offer.finance_charge or 0,
        monthly_payment=monthly_payment,
        total_of_payments=total_of_payments,
        disclosure=disclosure,
        schedule=[ScheduleRow(**r) for r in rows],
    )
