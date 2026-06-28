"""Pydantic response models for the LSS API."""
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class LoanListItem(BaseModel):
    id: int
    applicant_name: Optional[str] = None
    principal: float
    apr: float
    term_months: int
    status: Optional[str] = None
    balance: float = 0.0
    past_due: float = 0.0
    opened_at: Optional[str] = None


class LoanDetail(LoanListItem):
    pass


class BalanceOut(BaseModel):
    loan_id: int
    balance: float
    past_due: float = 0.0


class ScheduleRow(BaseModel):
    n: int
    due_date: str
    payment: float
    principal: float
    interest: float
    balance: float


class ScheduleOut(BaseModel):
    loan_id: int
    schedule: list[ScheduleRow]


class PaymentItem(BaseModel):
    id: int
    amount: float
    method: Optional[str] = None
    masked_pan: Optional[str] = None
    created_at: Optional[str] = None


class PaymentsOut(BaseModel):
    loan_id: int
    items: list[PaymentItem]


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
