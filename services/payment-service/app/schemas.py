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


class PaymentItem(BaseModel):
    id: int
    amount: float
    method: Optional[str] = None
    masked_pan: Optional[str] = None
    created_at: Optional[str] = None
