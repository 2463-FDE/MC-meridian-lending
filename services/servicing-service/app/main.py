"""Servicing service (LSS) — FastAPI.

Read API (loan list / detail / schedule / payment history) uses SQLAlchemy. The
money-moving endpoints (payments, balance adjust, fee waiver) keep their original raw
implementation.

Authorization arrived with ADR 0014 Decision 1 (see app/authz.py), closing debt D8(b):
adjust-balance and waive-fee require a servicing money role, apply-payment and late-fee
require the internal-service secret, and loan-scoped reads admit staff or the owning
borrower. What is still absent is a SECOND APPROVER on the two discretionary moves —
deferred to the next cycle by the client, with the design fixed in that ADR — and any
record of the movement, which is the ledger ADR 0014 Decision 3 specifies.
"""

import logging
import os
import re

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

from . import authz, balance, config, delinquency, payments, reconciliation
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
            content={
                "status": "unhealthy",
                "service": "servicing",
                "missing_secrets": missing,
            },
        )
    ok, db_error = config.database_reachable()
    if not ok:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "service": "servicing",
                "database_error": db_error,
            },
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
def post_payment(
    body: PaymentIn,
    x_user_role: Optional[str] = Header(None),
    x_user_id: Optional[str] = Header(None),
):
    # Same class of authorization failure closed elsewhere in this file (ADR 0014
    # Decision 1): a money role (CSR/admin), or the borrower who owns the loan
    # being charged. NOT any staff role -- charging a card is a money-moving write
    # like adjust-balance/waive-fee, not a read, so an underwriter (staff but not
    # a money role) does not get a blanket pass here. Denied as 404 so a serial
    # loan id cannot be probed for existence.
    authz.require_money_role_or_owner(body.loan_id, x_user_role, x_user_id)
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


# Same charset rule payment-service applies when it mints the id (its
# new_request_id): no whitespace, so a header value cannot forge a second log
# record, and capped so it cannot push the operational fields off the line.
_REQUEST_ID_OK = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _span_request_id(supplied: Optional[str]) -> str:
    """The correlation id for this log line, or "-" when there is none.

    Servicing has no id of its own to mint: it is the downstream half of a span
    payment-service opens. A direct call with no header is therefore logged as
    request_id=- -- present and visibly uncorrelated, rather than an omitted
    field that reads as a parse gap (test vector V-TRACE-DIRECT). A supplied
    value that fails the charset rule is treated the same way, since the
    alternative is writing client-controlled text into the log verbatim.
    """
    if supplied and _REQUEST_ID_OK.fullmatch(supplied):
        return supplied
    return "-"


@app.post("/accounts/{loan_id}/apply-payment")
def apply_payment(
    loan_id: int,
    body: ApplyPaymentIn,
    x_internal_service: Optional[str] = Header(
        default=None, alias="X-Internal-Service"
    ),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    # This is the apply path called by payment-service AFTER it captures the charge (the
    # LSS half of the split payment flow). Internal-only: it reduces a balance and is
    # reachable through the gateway on session auth alone, so without this gate a caller
    # could credit any balance with no card and no payments row — money creation, which
    # is a different problem from the authorization model (debt D8 split (a), ADR 0013).
    # The unlocked read-modify-write (D3) and the missing waterfall (D14) are unchanged
    # here: this commit gates the route, it does not fix the mutation.
    authz.require_internal_caller(x_internal_service)
    new_balance = balance.apply_payment(loan_id, body.amount)
    # The LSS half of the payment span. This handler logged NOTHING before, so a
    # payment crossing the seam left one line in payment-service and no
    # counterpart here at all. Logged AFTER the balance actually moves, never
    # before (criterion 3), carrying the same four named fields in the same order
    # payment-service uses, so one field query returns both halves.
    log.info(
        "apply-payment request_id=%s loan_id=%s payment_id=%s outcome=%s new_balance=%s",
        _span_request_id(x_request_id),
        loan_id,
        body.payment_id,
        "applied",
        new_balance,
    )
    return {
        "loan_id": loan_id,
        "applied_amount": body.amount,
        "new_balance": new_balance,
    }


@app.get("/accounts/{loan_id}/balance")
def get_account_balance(
    loan_id: int,
    x_user_role: Optional[str] = Header(None),
    x_user_id: Optional[str] = Header(None),
):
    # Staff, or the borrower who owns the loan. Denied as 404 so a serial id cannot be
    # probed for existence (ADR 0014 Decision 1).
    authz.require_staff_or_owner(loan_id, x_user_role, x_user_id)
    return {
        "loan_id": loan_id,
        "balance": balance.get_balance(loan_id),
        "past_due": balance.get_past_due(loan_id),
    }


class AdjustIn(BaseModel):
    new_balance: float


@app.post("/accounts/{loan_id}/adjust-balance")
def adjust_balance(
    loan_id: int, body: AdjustIn, x_user_role: Optional[str] = Header(None)
):
    # CSR or admin only (ADR 0014 Decision 1) — the header is now READ, not just
    # declared. Still no second approver and still no ledger entry: approval is deferred
    # to the next cycle by the client and the ledger is Decision 3. (debt D8(b) closed,
    # D2 open)
    authz.require_money_role(x_user_role)
    return {
        "loan_id": loan_id,
        "balance": balance.adjust_balance(loan_id, body.new_balance),
    }


class WaiveIn(BaseModel):
    amount: float


@app.post("/accounts/{loan_id}/waive-fee")
def waive_fee(loan_id: int, body: WaiveIn, x_user_role: Optional[str] = Header(None)):
    # CSR or admin only (ADR 0014 Decision 1). No amount limit is enforced: the
    # ops-manual $150-per-account-per-month guideline is recorded and displayed, not
    # gated, and enforcement is carded (docs/cards-week6-servicing.md C4). A manual
    # late-fee reversal comes through here rather than a separate flow.
    authz.require_money_role(x_user_role)
    return {"loan_id": loan_id, "past_due": balance.waive_fee(loan_id, body.amount)}


@app.post("/accounts/{loan_id}/late-fee")
def late_fee(
    loan_id: int,
    x_internal_service: Optional[str] = Header(
        default=None, alias="X-Internal-Service"
    ),
):
    # Rule-driven with no operator-chosen amount, so it is internal-only rather than
    # role-gated (ADR 0014 Decision 1). A representative reversing a late fee by hand
    # uses waive-fee, which records the reason.
    authz.require_internal_caller(x_internal_service)
    return {"loan_id": loan_id, "past_due": delinquency.assess_late_fee(loan_id)}


@app.get("/reconciliation/peek")
def reconciliation_peek(
    x_internal_service: Optional[str] = Header(
        default=None, alias="X-Internal-Service"
    ),
):
    # Not a real control — just exposes the two totals. They don't tie out. (debt D7)
    #
    # Internal-only, same pattern as apply-payment and late-fee (ADR 0014 Decision 1):
    # the value is compared with hmac.compare_digest, not merely required to be present,
    # and the gateway strips any client-supplied X-Internal-Service so it cannot be
    # forged from outside. This route reports across EVERY loan in the file, while a
    # borrower — on the same gateway, on the public internet — reads their own account
    # only. Whatever this returns, it is not one caller's own record.
    authz.require_internal_caller(x_internal_service)
    try:
        settlement = reconciliation.settlement_total()
    except reconciliation.ReconciliationAbort as exc:
        # The settlement file could not be read, so there is no total to report. A 200
        # carrying 0.0 here was the fail-open: a successful-looking answer for a file
        # nobody opened.
        log.error("reconciliation aborted: %s", exc)
        raise HTTPException(status_code=503, detail=f"reconciliation aborted: {exc}")
    return {
        "ledger_total": reconciliation.ledger_total(),
        "settlement_total": settlement,
    }
