"""Offer / Truth-in-Lending disclosure generation.

The offer build + APR/finance-charge + amortization logic was extracted into
disclosure-service. This router is now a thin pass-through: it calls disclosure-service
over HTTP and maps its response into the OfferOut shape the frontend already expects.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import clients
from ..schemas import Disclosure, OfferOut, ScheduleRow

router = APIRouter(tags=["offers"])


class OfferIn(BaseModel):
    app_id: int
    principal: float = Field(gt=0, le=50000)
    annual_rate_pct: float = Field(default=7.99, gt=0, le=35)
    term_months: int = Field(default=48, ge=12, le=60)


def _to_offer_out(app_id: int, resp: dict) -> OfferOut:
    """Map a disclosure-service OfferResponse into the LOS OfferOut/Disclosure shape."""
    d = resp.get("disclosure") or {}
    rows = resp.get("schedule") or d.get("schedule") or []
    disclosure = Disclosure(
        apr=d.get("apr", 0), finance_charge=d.get("finance_charge", 0),
        monthly_payment=d.get("monthly_payment", 0),
        amount_financed=d.get("amount_financed", 0),
        total_of_payments=d.get("total_of_payments", 0),
        schedule=[ScheduleRow(**row) for row in rows],
    )
    return OfferOut(app_id=app_id, disclosure=disclosure)


@router.post("/offer", response_model=OfferOut)
def make_offer(body: OfferIn):
    resp = clients.post(clients.DISCLOSURE_URL, "/offers", {
        "application_id": body.app_id,
        "principal": body.principal,
        "term_months": body.term_months,
        "annual_rate": body.annual_rate_pct,
    })
    return _to_offer_out(body.app_id, resp)


@router.get("/applications/{app_id}/offer", response_model=OfferOut)
def get_offer(app_id: int):
    resp = clients.get(clients.DISCLOSURE_URL, f"/applications/{app_id}/offer")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="no offer for this application")
    resp.raise_for_status()
    return _to_offer_out(app_id, resp.json())
