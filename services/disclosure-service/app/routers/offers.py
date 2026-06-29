"""Offer / Truth-in-Lending disclosure generation (disclosure-service).

Write path (POST /offers) builds the offer + amortization schedule with float math and
persists an offers row via raw psycopg2 (matches the LOS write path). Read path
(GET /applications/{id}/offer) goes through SQLAlchemy.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import db, models, offer as offer_mod, schedule
from ..database import get_session
from ..schemas import Disclosure, OfferIn, OfferResponse, ScheduleRow

router = APIRouter(tags=["offers"])


@router.post("/offers", response_model=OfferResponse)
def create_offer(body: OfferIn):
    o = offer_mod.build_offer(body.principal, body.annual_rate, body.term_months)
    rows = schedule.amortization(body.principal, body.annual_rate, body.term_months)
    # persist via raw psycopg2 (matches origination's write path) — float money columns
    inserted = db.query(
        "INSERT INTO offers (app_id, apr, finance_charge, monthly_payment, "
        "amount_financed, total_of_payments) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (body.application_id, o["apr"], o["finance_charge"], o["monthly_payment"],
         o["amount_financed"], o["total_of_payments"]),
    )
    offer_id = inserted[0]["id"]
    disclosure = Disclosure(
        apr=o["apr"], finance_charge=o["finance_charge"],
        monthly_payment=o["monthly_payment"], amount_financed=o["amount_financed"],
        total_of_payments=o["total_of_payments"],
    )
    return OfferResponse(
        offer_id=offer_id, application_id=body.application_id,
        apr=o["apr"], finance_charge=o["finance_charge"],
        monthly_payment=o["monthly_payment"], total_of_payments=o["total_of_payments"],
        disclosure=disclosure, schedule=[ScheduleRow(**r) for r in rows],
    )


@router.get("/applications/{application_id}/offer", response_model=OfferResponse)
def get_offer(application_id: int, session: Session = Depends(get_session)):
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
    principal = round(amount_financed / (1 - offer_mod.ORIGINATION_FEE_PCT), 2) if amount_financed else 0.0
    term_months = round(total_of_payments / monthly_payment) if monthly_payment else 0
    rows = schedule.amortization(principal, offer.apr or 7.99, term_months) if term_months else []
    disclosure = Disclosure(
        apr=offer.apr or 0, finance_charge=offer.finance_charge or 0,
        monthly_payment=monthly_payment, amount_financed=amount_financed,
        total_of_payments=total_of_payments,
    )
    return OfferResponse(
        offer_id=offer.id, application_id=application_id,
        apr=offer.apr or 0, finance_charge=offer.finance_charge or 0,
        monthly_payment=monthly_payment, total_of_payments=total_of_payments,
        disclosure=disclosure, schedule=[ScheduleRow(**r) for r in rows],
    )
