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

import uuid

from fastapi import FastAPI, Header, HTTPException, Request, Response
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
    response: Response,
    x_user_role: Optional[str] = Header(None),
    x_user_id: Optional[str] = Header(None),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
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
    # D19. This route is a SECOND front door onto the same payments table (debt D23),
    # and the client confirmed 2026-08-17 that it stays -- so it enforces the identical
    # contract. Deduping in one handler and not the other would leave the double charge
    # reachable through this one, which is exactly why the arbiter is a DB constraint.
    if not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key header is required (ADR 0013 Decision 1).",
        )
    try:
        # Canonicalize (lowercase, hyphenated): uuid.UUID() accepts hyphenless and
        # uppercase spellings as the same UUID, but the key is stored as raw TEXT, so
        # two spellings of one UUID would claim two distinct rows and dedupe nothing.
        idempotency_key = str(uuid.UUID(idempotency_key))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="Idempotency-Key must be a UUID.")
    # X-Request-Id enters the span here too -- this route is a second front
    # door for the same charge path as payment-service's /payments (D1(a)).
    result = payments.charge(
        body.loan_id,
        body.pan,
        body.cvv,
        body.amount,
        body.ssn,
        body.name,
        body.method,
        request_id=x_request_id,
        idempotency_key=idempotency_key,
    )
    if result.get("idempotency") == payments.FINGERPRINT_MISMATCH:
        raise HTTPException(
            status_code=422,
            detail=(
                "This Idempotency-Key was already used for a different payment. "
                "Use a new key for a new payment."
            ),
        )
    if result.get("idempotency") == payments.IN_FLIGHT:
        raise HTTPException(
            status_code=409,
            detail="A payment with this Idempotency-Key is still in progress.",
            headers={"Retry-After": "5"},
        )
    if result.get("idempotency") == payments.REPLAY:
        response.headers["Idempotent-Replay"] = "true"
    return result


class ApplyPaymentIn(BaseModel):
    # `amount` is deliberately absent (D3 / ADR 0020, spec D3(d) property 3). The amount
    # credited comes out of the `payments` row this id names; a caller-supplied figure was
    # a way to credit an amount that was never captured. Removing it is a breaking change
    # to the one caller (payment-service), which moves in the same PR.
    payment_id: int


# Same two rules payment-service applies to the header (its new_request_id):
# a charset with no whitespace, so a header value cannot forge a second log
# record and cannot push the operational fields off the line; and a 9-digit
# ceiling, since an SSN is 9 digits and a card 13-19, and the redactor masks a
# bare SSN only inside a labeled field and a bare PAN only when Luhn-valid --
# neither covers request_id=412559981. Counting digits rather than matching a
# shape is what makes separator- and letter-padded variants fail too. This route
# is reachable directly (internal token only), so the rule cannot be left to the
# upstream service. (Codex review)
#
# Sourced from payments.py (already imported below) rather than redefined here:
# this module's own POST /payments route mints ids with the identical rule via
# payments.new_request_id, and a second hand-copy of the same regex/ceiling in
# this file is exactly the drift risk a prior review round found between the
# two services -- don't reintroduce it within one.
_REQUEST_ID_OK = payments._REQUEST_ID_OK
_MAX_REQUEST_ID_DIGITS = payments._MAX_REQUEST_ID_DIGITS
_ASCII_DIGIT = payments._ASCII_DIGIT


def _span_request_id(supplied: Optional[str]) -> str:
    """The correlation id for this log line, or "-" when there is none.

    Servicing has no id of its own to mint: it is the downstream half of a span
    payment-service opens. A direct call with no header is therefore logged as
    request_id=- -- present and visibly uncorrelated, rather than an omitted
    field that reads as a parse gap (test vector V-TRACE-DIRECT). A supplied
    value that fails the charset rule or carries SSN/card-length digits is
    treated the same way, since the alternative is writing client-controlled
    text into the log verbatim.
    """
    if (
        supplied
        and _REQUEST_ID_OK.fullmatch(supplied)
        and len(_ASCII_DIGIT.findall(supplied)) < _MAX_REQUEST_ID_DIGITS
    ):
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
    # The mutation is atomic as of D3 (ADR 0020): the application record and the balance
    # movement commit together, and the amount comes from the payments row rather than
    # from this body. The missing waterfall (D14) is still unchanged here.
    authz.require_internal_caller(x_internal_service)
    effective_request_id = _span_request_id(x_request_id)
    try:
        new_balance, moved = balance.apply_payment(
            loan_id,
            body.payment_id,
            request_id=effective_request_id,
        )
    except balance.PaymentNotApplicable as exc:
        # 422, not 404 or 500: the request is well-formed and the caller is authorized,
        # but this payment does not credit this loan and NOTHING was written. The one
        # caller reads any non-2xx as "not applied" and finalizes the row
        # captured_unapplied, which is the honest state — card charged, balance unmoved.
        log.warning(
            "apply-payment refused request_id=%s loan_id=%s payment_id=%s outcome=%s",
            effective_request_id,
            loan_id,
            body.payment_id,
            exc.reason,
        )
        raise HTTPException(status_code=422, detail=exc.reason)
    # The LSS half of the payment span. This handler logged NOTHING before, so a
    # payment crossing the seam left one line in payment-service and no
    # counterpart here at all. Logged AFTER the balance actually moves, never
    # before (criterion 3), carrying the same four named fields in the same order
    # payment-service uses, so one field query returns both halves.
    log.info(
        "apply-payment request_id=%s loan_id=%s payment_id=%s outcome=%s new_balance=%s",
        effective_request_id,
        loan_id,
        body.payment_id,
        "applied" if moved else "already_applied",
        new_balance,
    )
    return {
        "loan_id": loan_id,
        # Whether THIS call moved the balance. A replay is a 200 with moved=false: the
        # money is on the loan, so the caller has succeeded, but it credited nothing now.
        # The caller must not add this to a running total.
        "moved": moved,
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
    """Break summary for the settlement file's own window (D2).

    Internal-only, same pattern as apply-payment and late-fee (ADR 0014 Decision 1):
    the value is compared with hmac.compare_digest, not merely required to be present,
    and the gateway strips any client-supplied X-Internal-Service so it cannot be
    forged from outside. This route reports across EVERY loan in the window, while a
    borrower — on the same gateway, on the public internet — reads their own account
    only.

    It returns the SAME document `python -m app.reconcile` writes to stdout (D3(d)),
    built by `reconciliation.build_report`. Keeping a second, weaker comparison beside
    the real one is the drift the fee-schedule loader was built to end.

    An abort is a 503, never a 200 carrying zeroes. The command in D3(a) is the
    interface for this control; this endpoint is a legacy caller kept working.
    """
    authz.require_internal_caller(x_internal_service)
    try:
        result = reconciliation.reconcile()
    except reconciliation.ReconciliationAbort as exc:
        log.error("reconciliation aborted: %s", exc)
        raise HTTPException(status_code=503, detail=f"reconciliation aborted: {exc}")
    return reconciliation.build_report(result)
