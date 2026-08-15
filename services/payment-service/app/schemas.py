"""Pydantic models for the Payment Service API."""

from typing import Optional

from pydantic import BaseModel


class PaymentIn(BaseModel):
    loan_id: int
    pan: Optional[str] = None
    cvv: Optional[str] = None
    amount: float
    ssn: Optional[str] = None
    name: Optional[str] = None
    method: str = "card"


class PaymentOut(BaseModel):
    payment_id: Optional[int] = None
    loan_id: int
    status: str
    applied_amount: float
    # The effective span id (caller's own, or the generated replacement when the
    # caller's was refused -- see payments.new_request_id). Without this the
    # caller has no way to learn the replacement id and cannot correlate its own
    # logs with payment/servicing's (Codex review).
    request_id: Optional[str] = None


class PaymentItem(BaseModel):
    id: int
    amount: float
    method: Optional[str] = None
    masked_pan: Optional[str] = None
    created_at: Optional[str] = None
