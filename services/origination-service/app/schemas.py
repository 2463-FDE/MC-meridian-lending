"""Pydantic request/response models for the LOS API."""

import re
from datetime import date
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, Field, field_validator, model_validator

T = TypeVar("T")

# Earliest DOB accepted. Postgres DATE reaches year 294276 and Python's date stops at
# 9999, so an out-of-range year is not merely implausible data -- it is unreadable data
# (see _validate_dob). 1900 is the floor because no lending applicant predates it.
_DOB_MIN_YEAR = 1900

# 9 bare digits or fully-dashed ###-##-####, nothing else. The alternation forces the
# dashes all-or-nothing: an independently-optional \d{3}-?\d{2}-?\d{4} would accept
# partially-dashed junk like 412-559980 / 41255-9980, which would then reach storage and
# KYC (whose stub verifies any non-empty SSN). Reject at the API boundary so malformed
# SSNs never hit storage or the log redactor, whose separator handling this branch hardens
# (fix/redactor-ssn-separator-blindspots). Mirrors the apply-form client check; the client
# gate is UX, this is the enforced one.
_SSN_RE = re.compile(r"^(?:\d{9}|\d{3}-\d{2}-\d{4})$")

# Anchored NANP allowlist mirroring the labeled-phone redactor (redactor.py rule 5a:
# \+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}). A digit-count-only check accepted
# junk wrappers the redactor does not mask -- "abc5555550123" and "555::::123::::4567"
# both carry 10 digits but sit outside the redactor's narrow shape, so once labeled they
# survive redaction into logs/provider payloads/storage (PR review). Anchoring to the
# redactor's own pattern makes every accepted value one the redactor can mask, holding
# the boundary invariant that junk does not pass.
_PHONE_RE = re.compile(r"^\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}$")

# Canonical YYYY-MM-DD only. date.fromisoformat is NOT a shape check: since Python 3.11 it
# accepts most of ISO 8601, including the basic form "19900422" and week dates "2021-W01-1"
# / "1990-W01". _validate_dob returns the raw string, so those reach the applicants.dob
# DATE column verbatim -- Postgres coerces the basic form (storing a value that never
# matched the documented shape) and rejects the week form with "invalid input syntax for
# type date", which surfaces as a 500 inside the intake transaction instead of a 422 (PR
# review). Gate on the shape first so only the shape the error message and the apply form
# promise can reach fromisoformat.
_DOB_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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

    @field_validator("dob")
    @classmethod
    def _validate_dob(cls, v: Optional[str]) -> Optional[str]:
        # Optional: entity applicants carry an EIN and no DOB (see _entity_requires_ein),
        # so only a present, non-blank value is checked.
        #
        # This is a READ-PATH availability guard, not just input hygiene. `applicants.dob`
        # is a Postgres DATE, which accepts years up to 294276, but Python's `date` stops
        # at 9999 -- so a year outside that range stores fine and then raises
        # `ValueError: year N is out of range` when SQLAlchemy builds the Python object.
        # That happens during row hydration, so it takes down `GET /los/applications` --
        # the whole officer queue, for every officer, over ONE bad row -- not merely the
        # application that carries it. A typed "21990-04-22" in the apply form's native
        # date input did exactly this. Reject at the boundary so an unreadable date can
        # never reach storage.
        #
        # NORMALIZE by returning the stripped value, matching the ssn/phone validators:
        # date.fromisoformat rejects surrounding whitespace outright, so stripping first
        # keeps " 1990-04-22 " accepted while ensuring only a canonical ISO date is stored.
        if v is None:
            return v
        v = v.strip()
        if not v:
            # Blank means absent, not malformed -- dob is optional (entity applicants carry
            # an EIN instead). Return None so it lands as SQL NULL: an empty string reaches
            # the applicants.dob DATE column as "" and Postgres raises "invalid input syntax
            # for type date", which is a 500 inside the intake transaction rather than the
            # 422 this validator exists to produce (PR review). None is also what the KYC
            # call already expects for a missing DOB (routers/applications.py::run_kyc).
            return None
        if not _DOB_RE.fullmatch(v):
            # Shape gate ahead of fromisoformat: rejects the 5-digit year, a 3-digit year,
            # single-digit month/day, and the ISO variants fromisoformat would otherwise
            # accept and hand on unnormalized (see _DOB_RE).
            raise ValueError("dob must be a calendar date as YYYY-MM-DD")
        try:
            parsed = date.fromisoformat(v)
        except ValueError:
            # Right shape, impossible date: month 13, February 30. The DATE column would
            # reject these too, but as a 500 rather than a 422.
            raise ValueError("dob must be a calendar date as YYYY-MM-DD") from None
        if parsed.year < _DOB_MIN_YEAR:
            raise ValueError(f"dob year must be {_DOB_MIN_YEAR} or later")
        if parsed > date.today():
            raise ValueError("dob cannot be in the future")
        # Deliberately NOT enforcing the minimum age here. `policies/underwriting_guidelines.md`
        # sets it at 18, but that is an eligibility decision the underwriting path owns --
        # rejecting a 17-year-old as a malformed request would report a policy outcome as a
        # format error and skip the adverse-action record it deserves.
        return v

    @field_validator("phone")
    @classmethod
    def _validate_phone(cls, v: Optional[str]) -> Optional[str]:
        # Optional; when present match the anchored _PHONE_RE allowlist so
        # (555) 555-0123, 555-555-0123, and 5555550123 all pass but junk does not.
        # A digit-count-only check accepted values the labeled-phone redactor cannot
        # mask ("abc5555550123", "555::::123::::4567" both have 10 digits but fall
        # outside the redactor's NANP shape), opening a PII leak path once such a value
        # is labeled downstream (PR review). Anchor to the redactor's own pattern so
        # accepted values are always maskable. NORMALIZE by returning the stripped value:
        # the earlier check ignored surrounding whitespace, so " 5555550123 " passed and
        # model_dump() preserved the padding, forwarding/storing a malformed phone.
        # Strip so only the padding is removed; internal formatting is left intact.
        if v is None:
            return v
        v = v.strip()
        if v and not _PHONE_RE.match(v):
            raise ValueError("phone must be 10 digits in a standard US format")
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
    # First principal reason only (legacy field, kept for callers reading it). Mirrors
    # decision-service's DecisionOut.reason.
    adverse_action_reason: Optional[str] = None
    # Every specific Reg B principal reason, ranked worst-first: [{code, reason, feature}].
    # 12 CFR 1002.9 requires the reason(s) actually used, and decision-service ranks up to
    # four; forwarding only the legacy field above told an applicant denied for three
    # reasons about one of them while decision_events recorded all three.
    principal_reasons: list = []


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


class PrincipalReason(BaseModel):
    # Allowlisted shape for a decision_events.principal_reasons item (Codex review):
    # that column is unconstrained JSONB, and this route is the first place a legacy/
    # backfilled/hand-edited row's reasons reach a borrower-readable response. A bare
    # `list` field forwards whatever extra keys the row happens to carry; this schema
    # drops anything not in {code, reason, feature}.
    code: Optional[str] = None
    reason: Optional[str] = None
    feature: Optional[str] = None


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
    # Latest decision_events row's score/reasons (PR review): decision was outcome-only,
    # so resuming a denied application, or an officer opening one without rerunning
    # decisioning, showed the status with no Reg B reasons. Same shape as DecisionOut.
    score: Optional[int] = None
    adverse_action_reason: Optional[str] = None
    principal_reasons: list[PrincipalReason] = []
    offer: Optional[Disclosure] = None


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
