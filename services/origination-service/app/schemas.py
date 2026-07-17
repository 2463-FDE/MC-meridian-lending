"""Pydantic request/response models for the LOS API."""

from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApplicationIn(BaseModel):
    name: str = Field(min_length=1)
    dob: Optional[str] = None
    ssn: Optional[str] = None
    ein: Optional[str] = None
    is_entity: bool = False
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    amount: float = Field(gt=0, le=50000)
    term_months: int = Field(default=36, ge=12, le=60)
    purpose: Optional[str] = None
    income: Optional[float] = Field(default=None, ge=0)
    # Required underwriting input: the model scores debt-to-income from it, so a
    # missing value must be rejected at the API boundary rather than silently scored
    # as zero debt (over-approval risk, PR #7 review). Explicit 0 is allowed.
    monthly_debt: float = Field(ge=0)
    employer: Optional[str] = None
    job_title: Optional[str] = None
    employment_years: Optional[float] = Field(default=None, ge=0)


class MonthlyDebtIn(BaseModel):
    # Remediation capture for a quarantined row: a legacy/seeded application with
    # NULL monthly_debt is rejected at decisioning (422) with "must be captured
    # before a decision can be made"; this is the path that captures it. Same
    # ge=0 rule as ApplicationIn.monthly_debt (explicit 0 allowed).
    monthly_debt: float = Field(ge=0)


class KycOut(BaseModel):
    name_verified: bool
    dob_verified: bool
    address_verified: bool
    ssn_verified: bool


class ApplicationCreated(BaseModel):
    app_id: int
    status: str
    kyc: KycOut
    # False when the KYC service call did not complete (outage/timeout/auth failure) —
    # distinct from a KYC that ran and returned all-false. Lets a caller tell "not
    # verified" from "verification could not be performed" (PR review). Default True so
    # the field is backward-compatible for existing consumers.
    kyc_checked: bool = True
    # ADR 0010 Phase B: unguessable per-application continuation token (see authz.py). The
    # anonymous applicant must send it as X-Application-Token to complete decision/offer/
    # accept on this application. None for officer-created flows (the officer is already
    # authorized by role). Bearer capability — the client holds it like a magic link.
    continuation_token: str | None = None


class ApplicantOut(BaseModel):
    id: int
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    is_entity: bool = False


class ApplicationListItem(BaseModel):
    id: int
    applicant_name: Optional[str] = None
    amount: float
    term_months: int
    purpose: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[str] = None


class DecisionOut(BaseModel):
    app_id: int
    decision: str
    score: int
    adverse_action_reason: Optional[str] = None


class ScheduleRow(BaseModel):
    n: int
    due_date: str
    payment: float
    principal: float
    interest: float
    balance: float


class Disclosure(BaseModel):
    apr: float
    finance_charge: float
    monthly_payment: float
    amount_financed: float
    total_of_payments: float
    schedule: list[ScheduleRow] = []


class OfferOut(BaseModel):
    app_id: int
    disclosure: Disclosure


class ApplicationDetail(BaseModel):
    id: int
    applicant: Optional[ApplicantOut] = None
    amount: float
    term_months: int
    purpose: Optional[str] = None
    status: Optional[str] = None
    employer: Optional[str] = None
    job_title: Optional[str] = None
    kyc: Optional[KycOut] = None
    decision: Optional[str] = None
    offer: Optional[Disclosure] = None


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
