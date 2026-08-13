"""Payment capture API. POST /payments charges a card/ACH and applies it to the balance."""

from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from .. import authz, config, payments
from ..schemas import PaymentIn, PaymentOut

router = APIRouter(tags=["payments"])


def _mask_pan(pan: str | None) -> str | None:
    # Display-only helper. The stored payments row and the payment log keep the FULL PAN
    # and CVV (PCI debt) — masking is never applied to what this service persists.
    if not pan:
        return None
    return "•••• " + pan[-4:]


@router.post("/payments", response_model=PaymentOut)
def post_payment(
    body: PaymentIn,
    x_user_role: Optional[str] = Header(None),
    x_user_id: Optional[str] = Header(None),
):
    # This is the front-door capture route the gateway/frontend actually call --
    # servicing-service's own /payments already gates on money-role-or-owner
    # (ADR 0014 Decision 1); without the same gate here, any authenticated
    # caller could capture a charge against any loan id and ride the internal
    # token past servicing's gate as a confused deputy. Denied as 404, matching
    # servicing-service's own /payments (no existence oracle on loan id).
    authz.require_money_role_or_owner(body.loan_id, x_user_role, x_user_id)
    # Fail closed without a processor credential: charge() would record a
    # 'captured' payment no processor ever authorized. (readiness also flags this)
    if not config.processor_configured():
        raise HTTPException(
            status_code=503,
            detail="payment processor not configured (PROCESSOR_API_KEY unset)",
        )
    # Fail closed on a missing/non-ASCII internal-service token too: without it,
    # charge() captures the payment and _apply_via_servicing's call to servicing
    # either gets denied (403) or, for a non-ASCII token, raises UnicodeEncodeError
    # inside httpx -- both swallowed by the same broad except, leaving the balance
    # unapplied behind a "captured" response. (readiness also flags this)
    if not config.internal_service_token_configured():
        raise HTTPException(
            status_code=503,
            detail="internal service token not configured (INTERNAL_SERVICE_TOKEN "
            "unset or non-ASCII)",
        )
    # No idempotency key accepted or checked. Retried POST = second charge. (debt D2)
    return payments.charge(
        body.loan_id, body.pan, body.cvv, body.amount, body.ssn, body.name, body.method
    )
