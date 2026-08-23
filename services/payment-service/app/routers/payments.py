"""Payment capture API. POST /payments charges a card/ACH and applies it to the balance."""

import uuid
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Response

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
    response: Response,
    x_user_role: Optional[str] = Header(None),
    x_user_id: Optional[str] = Header(None),
    x_request_id: Optional[str] = Header(None),
    idempotency_key: Optional[str] = Header(None),
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
    # inside httpx -- both now correctly report captured_unapplied (below), but
    # catching it here instead gives a clear config diagnostic at request time
    # rather than a 424 on every single payment attempt. (readiness also flags this)
    if not config.internal_service_token_configured():
        raise HTTPException(
            status_code=503,
            detail="internal service token not configured (INTERNAL_SERVICE_TOKEN "
            "unset or non-ASCII)",
        )
    # D19. The key is REQUIRED and client-minted (ADR 0013 Decision 1): a server-minted
    # key would be different on every retry and would deduplicate nothing, which is the
    # whole point. Refused BEFORE any capture work, so a malformed request never reaches
    # the card.
    #
    # The UUID check is not decoration. The key is the sole arbiter of "same payment",
    # so a client sending a constant string ("retry", "1") would collapse genuinely
    # distinct payments into one -- refusing that request is the safe direction, and a
    # UUID is what the draft idempotency-key header specifies.
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
    # X-Request-Id enters the span here: charge() uses a caller-supplied id
    # verbatim and mints one otherwise, so the charge line, the apply call and
    # servicing's own line all come back under a single id (spec D1(a)).
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
    # D1's answers for a caller that did not win the key. None of these captured
    # anything, so none of them is a 424 (which reports a capture that did not apply).
    if result.get("idempotency") == payments.FINGERPRINT_MISMATCH:
        # A client defect, not a retry: the same key arrived carrying a different
        # payload. 422 rather than 409, because retrying THIS request under THIS key can
        # never succeed. Follows draft-ietf-httpapi-idempotency-key-header.
        raise HTTPException(
            status_code=422,
            detail=(
                "This Idempotency-Key was already used for a different payment. "
                "Use a new key for a new payment."
            ),
        )
    if result.get("idempotency") == payments.IN_FLIGHT:
        # The prior intent under this key has not finished. Exactly one processor call
        # happens per key, so this caller waits rather than charging in parallel.
        raise HTTPException(
            status_code=409,
            detail="A payment with this Idempotency-Key is still in progress.",
            headers={"Retry-After": "5"},
        )
    # Checked BEFORE the REPLAY return: a replay of a request that first went
    # captured_unapplied carries that same status on `result` (payments.charge's
    # REPLAY branch reconstructs it from the row), so this must fire on the retry
    # too, not just on the original request -- otherwise a REPLAY returns here
    # first and comes back 200, indistinguishable from a real success to the
    # existing frontend, which discards the response body and shows a flat
    # "submitted" message on any non-throwing call (B1, carried). 502/503/504
    # were ruled out even though the card WAS already captured: those codes
    # carry a "transient upstream hiccup, safe to retry" convention that
    # generic HTTP clients, gateways, and retry libraries act on regardless of
    # the detail text -- and with no idempotency key (D2/D19, tracked as its
    # own PR), an automated retry on this exact request would double-charge
    # the card. 424 Failed Dependency says precisely what happened (this
    # request's own work -- the capture -- succeeded; it failed because a
    # dependency -- servicing's apply call -- did not) without implying a
    # transient failure worth retrying (Codex review, PR 32, second pass on
    # this same fix).
    if result["status"] == "captured_unapplied":
        raise HTTPException(
            status_code=424,
            detail=(
                f"Payment captured (payment_id={result['payment_id']}, "
                f"request_id={result.get('request_id')}) but could not be "
                "applied to your balance. Do not retry -- contact support to "
                "reconcile this payment."
            ),
            # HTTPException bypasses the `response` dependency's headers entirely
            # (FastAPI builds a fresh JSONResponse from the exception), so the
            # replay marker has to travel on the exception itself, not on
            # `response.headers` the way the plain-200 REPLAY path below does.
            headers={"Idempotent-Replay": "true"}
            if result.get("idempotency") == payments.REPLAY
            else None,
        )
    if result.get("idempotency") == payments.REPLAY:
        # The original status and body, plus this one header -- so a client ignoring it
        # sees an identical result.
        response.headers["Idempotent-Replay"] = "true"
    return result
