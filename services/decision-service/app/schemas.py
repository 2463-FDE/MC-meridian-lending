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
    employment_years: float = Field(default=0, ge=0)
    # When the bureau provides a score it flows through the synchronous chain instead.
    credit_score: Optional[int] = None


class DecisionOut(BaseModel):
    application_id: int
    outcome: str
    score: float
    # First principal reason text (legacy field, kept for callers reading `reason`).
    reason: Optional[str] = None
    policy_band: Optional[str] = None
    # Specific Reg B principal reasons: [{code, reason, feature}, ...] (ADR 0009 §3).
    principal_reasons: list = []
    decided_by: Optional[str] = None
