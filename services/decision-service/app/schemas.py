"""Pydantic request/response models for the decision-service API."""
from typing import Optional

from pydantic import BaseModel, Field


class DecisionIn(BaseModel):
    application_id: int
    applicant_id: int
    name: str = Field(min_length=1)
    ssn: str
    requested_amount: float = Field(gt=0)
    term_months: int = Field(ge=12, le=60)
    annual_income: float = Field(ge=0)
    monthly_debt: float = Field(ge=0)
    # The client keeps asking for a smarter "AI" model — that work has not started.
    # When the bureau provides a score it flows through the synchronous chain instead.
    credit_score: Optional[int] = None


class DecisionOut(BaseModel):
    application_id: int
    outcome: str
    score: float
    reason: Optional[str] = None
