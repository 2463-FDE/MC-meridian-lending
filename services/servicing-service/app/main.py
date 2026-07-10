"""Servicing service (LSS) — FastAPI.

Read API (loan list / detail / schedule / payment history) uses SQLAlchemy. The
money-moving endpoints (payments, balance adjust, fee waiver) keep their original raw
implementation and accept ANY authenticated caller — no role check, no maker-checker.
(weak authz — kept on purpose)
"""
import logging
import os

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

from . import balance, config, delinquency, payments, reconciliation
from .logging_config import get_logger
from .routers import loans

log = get_logger("servicing")

app = FastAPI(title="Meridian Servicing Service (LSS)", version="2.0.0")
app.include_router(loans.router)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.error("unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "internal error"})


@app.get("/health")
def health():
    missing = config.missing_required_secrets()
    if missing:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "service": "servicing", "missing_secrets": missing},
        )
    ok, db_error = config.database_reachable()
    if not ok:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "service": "servicing", "database_error": db_error},
        )
    return {"status": "ok", "service": "servicing"}


class PaymentIn(BaseModel):
    loan_id: int
    pan: Optional[str] = None
    cvv: Optional[str] = None
    amount: float
    ssn: Optional[str] = None
    name: Optional[str] = None
    method: str = "card"


@app.post("/payments")
def post_payment(body: PaymentIn):
    # Fail closed without a processor credential: charge() inserts the payment and
    # mutates the balance, recording a 'captured' payment no processor authorized.
    if not config.processor_configured():
        raise HTTPException(
            status_code=503,
            detail="payment processor not configured (PROCESSOR_API_KEY unset)",
        )
    # No idempotency key accepted or checked. Retried POST = second charge. (debt D2)
    return payments.charge(
        body.loan_id, body.pan, body.cvv, body.amount, body.ssn, body.name, body.method
    )


class ApplyPaymentIn(BaseModel):
    amount: float
    payment_id: int


@app.post("/accounts/{loan_id}/apply-payment")
def apply_payment(loan_id: int, body: ApplyPaymentIn):
    # This is the apply path called by payment-service AFTER it captures the charge (the
    # LSS half of the split payment flow). It still does the unlocked read-modify-write
    # (D3) straight off principal with no waterfall (D14) — preserved exactly as-is.
    new_balance = balance.apply_payment(loan_id, body.amount)
    return {"loan_id": loan_id, "applied_amount": body.amount, "new_balance": new_balance}


@app.get("/accounts/{loan_id}/balance")
def get_account_balance(loan_id: int):
    return {
        "loan_id": loan_id,
        "balance": balance.get_balance(loan_id),
        "past_due": balance.get_past_due(loan_id),
    }


class AdjustIn(BaseModel):
    new_balance: float


@app.post("/accounts/{loan_id}/adjust-balance")
def adjust_balance(loan_id: int, body: AdjustIn,
                   x_user_role: Optional[str] = Header(None)):
    # ANY authenticated user. No role check, no second approver, no ledger entry. (debt D8)
    return {"loan_id": loan_id, "balance": balance.adjust_balance(loan_id, body.new_balance)}


class WaiveIn(BaseModel):
    amount: float


@app.post("/accounts/{loan_id}/waive-fee")
def waive_fee(loan_id: int, body: WaiveIn,
              x_user_role: Optional[str] = Header(None)):
    # ANY authenticated user can waive a fee. No maker-checker. (debt D8)
    return {"loan_id": loan_id, "past_due": balance.waive_fee(loan_id, body.amount)}


@app.post("/accounts/{loan_id}/late-fee")
def late_fee(loan_id: int):
    return {"loan_id": loan_id, "past_due": delinquency.assess_late_fee(loan_id)}


@app.get("/reconciliation/peek")
def reconciliation_peek():
    # Not a real control — just exposes the two totals. They don't tie out. (debt D7)
    return {
        "ledger_total": reconciliation.ledger_total(),
        "settlement_total": reconciliation.settlement_total(),
    }
