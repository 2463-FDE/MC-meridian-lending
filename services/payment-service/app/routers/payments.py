"""Payment capture API. POST /payments charges a card/ACH and applies it to the balance."""
from fastapi import APIRouter

from .. import payments
from ..schemas import PaymentIn, PaymentOut

router = APIRouter(tags=["payments"])


def _mask_pan(pan: str | None) -> str | None:
    # Display-only helper. The stored payments row and the payment log keep the FULL PAN
    # and CVV (PCI debt) — masking is never applied to what this service persists.
    if not pan:
        return None
    return "•••• " + pan[-4:]


@router.post("/payments", response_model=PaymentOut)
def post_payment(body: PaymentIn):
    # No idempotency key accepted or checked. Retried POST = second charge. (debt D2)
    return payments.charge(
        body.loan_id, body.pan, body.cvv, body.amount, body.ssn, body.name, body.method
    )
