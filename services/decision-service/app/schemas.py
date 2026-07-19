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
    # Optional idempotency key: a retry with the same id replays the recorded
    # decision (no second bureau pull / event). Absent = explicit re-decision.
    request_id: Optional[str] = Field(default=None, max_length=64)


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


class DecisionRecordOut(BaseModel):
    """Identifier-free projection of the latest decision event (ADR 0009 §4).

    status is "recorded" when an event exists; "no_record_legacy" when only a
    pre-feature outcome row exists — reasons for those were never captured and are
    unrecoverable (ADR 0008 req. 4). Distinct from 404 (never decisioned).
    """

    application_id: int
    status: str
    outcome: Optional[str] = None
    principal_reasons: list = []
    drivers: dict = {}
    policy_band: Optional[str] = None
    # NOTE: the persisted decision_events row also stores `inputs` (income/debt/amount)
    # as the append-only audit trail, but this projection deliberately does NOT return
    # them. The only caller (the officer assistant's memory tool, origination
    # assistant.py) reads outcome/policy_band/score/reason_codes and never the raw
    # financials, and the endpoint is reachable anonymously through the gateway
    # `/decision/*` proxy with enumerable app ids — so projecting the applicant's
    # financial inputs here would be a needless data leak (PR review).
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
