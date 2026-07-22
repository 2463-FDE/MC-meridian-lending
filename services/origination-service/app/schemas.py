"""Pydantic request/response models for the LOS API."""

import re
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, Field, field_validator, model_validator

T = TypeVar("T")

# 9 bare digits or fully-dashed ###-##-####, nothing else. The alternation forces the
# dashes all-or-nothing: an independently-optional \d{3}-?\d{2}-?\d{4} would accept
# partially-dashed junk like 412-559980 / 41255-9980, which would then reach storage and
# KYC (whose stub verifies any non-empty SSN). Reject at the API boundary so malformed
# SSNs never hit storage or the log redactor, whose separator handling this branch hardens
# (fix/redactor-ssn-separator-blindspots). Mirrors the apply-form client check; the client
# gate is UX, this is the enforced one.
_SSN_RE = re.compile(r"^(?:\d{9}|\d{3}-\d{2}-\d{4})$")


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

    @field_validator("ssn")
    @classmethod
    def _validate_ssn(cls, v: Optional[str]) -> Optional[str]:
        # Optional: entity applicants carry an EIN, not an SSN (see _entity_requires_ein),
        # so only a present, non-blank value is format-checked. Rejects the whitespace/
        # separator noise the redactor would otherwise have to absorb downstream.
        # NORMALIZE by returning the stripped value: matching _SSN_RE against v.strip()
        # while returning the raw v let a padded-but-valid SSN (" 412559980 ") pass and
        # be preserved by model_dump(), forwarding/storing a malformed SSN and leaving
        # the labeled value for the log redactor to catch. Strip here so the boundary
        # invariant holds and only a canonical SSN leaves this validator.
        if v is None:
            return v
        v = v.strip()
        if v and not _SSN_RE.match(v):
            raise ValueError("ssn must be 9 digits, optionally as ###-##-####")
        return v

    @field_validator("phone")
    @classmethod
    def _validate_phone(cls, v: Optional[str]) -> Optional[str]:
        # Optional; when present require exactly 10 digits ignoring formatting, so
        # (555) 555-0123, 555-555-0123, and 5555550123 all pass but junk does not.
        # NORMALIZE by returning the stripped value: same blindspot as _validate_ssn
        # -- the digit count ignores surrounding whitespace, so " 5555550123 " passed
        # and model_dump() preserved the padding, forwarding/storing a malformed phone.
        # Strip so only the padding is removed; internal formatting is left intact.
        if v is None:
            return v
        v = v.strip()
        if v and len(re.sub(r"\D", "", v)) != 10:
            raise ValueError("phone must contain 10 digits")
        return v

    @model_validator(mode="after")
    def _entity_requires_ein(self) -> "ApplicationIn":
        # is_entity is applicant-supplied and drops the natural-person DOB/SSN
        # requirement at the KYC gate (kyc_gate.require_kyc_passed). Without this an
        # applicant self-declares is_entity=true and clears KYC with no identity
        # element at all. Require an EIN for the entity carve-out so the claim costs
        # an identifier, not a free boolean. (Presence only -- run_cip depth is D11.)
        if self.is_entity and not (self.ein and self.ein.strip()):
            raise ValueError("is_entity requires an ein")
        return self


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
