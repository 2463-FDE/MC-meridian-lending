"""Origination service (LOS) — FastAPI.

Endpoints: application intake, KYC (CIP), decisioning, offer/disclosure.
"""
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional

from . import intake, kyc, decision
from .logging_config import get_logger

log = get_logger("origination")
app = FastAPI(title="Meridian Origination Service (LOS)", version="1.0.0")


class ApplicationIn(BaseModel):
    name: str
    dob: Optional[str] = None
    ssn: Optional[str] = None
    ein: Optional[str] = None
    is_entity: bool = False
    address: Optional[str] = None
    amount: float
    term_months: int = 36
    purpose: Optional[str] = None
    income: Optional[float] = None


@app.get("/health")
def health():
    return {"status": "ok", "service": "origination"}


@app.post("/applications")
def submit_application(body: ApplicationIn):
    payload = body.model_dump()
    app_id = intake.create_application(payload)
    cip = kyc.run_cip(payload)  # CIP only — no sanctions/UBO/monitoring
    return {"app_id": app_id, "kyc": cip}


@app.get("/decision/{app_id}")
def get_decision(app_id: int):
    # synchronous decisioning chain on the request thread
    application = {"app_id": app_id, "ssn": "", "income": 50000}
    return decision.decide(application)


class OfferIn(BaseModel):
    app_id: int
    principal: float
    annual_rate_pct: float = 7.99
    term_months: int = 48


@app.post("/offer")
def make_offer(body: OfferIn):
    o = intake.build_disclosure(body.app_id, body.principal, body.annual_rate_pct,
                                body.term_months)
    return {"app_id": body.app_id, "disclosure": o}


class BoardIn(BaseModel):
    app_id: int
    applicant_name: str
    principal: float
    annual_rate_pct: float = 7.99
    term_months: int = 48


@app.post("/board")
def board(body: BoardIn):
    loan_id = intake.board_to_servicing(
        body.app_id, body.applicant_name, body.principal,
        body.annual_rate_pct, body.term_months,
    )
    return {"loan_id": loan_id}
