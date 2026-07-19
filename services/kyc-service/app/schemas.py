"""Pydantic request/response models for the KYC API."""

from typing import Optional

from pydantic import BaseModel


class CipCheckIn(BaseModel):
    application_id: int
    applicant_id: int
    name: str
    # dob/ssn/address are optional to match origination's ApplicationIn (PR review): an
    # entity applicant (LLC) legitimately has no dob/ssn, and origination forwards whatever
    # was captured. run_cip treats a missing field as unverified (bool(None) is False), so a
    # partial/entity request still produces a persisted pass/fail kyc_checks row instead of a
    # 422 that origination misclassifies as KYC unavailability -- which, under the mandatory
    # persisted-KYC gate (ADR 0011), would strand the application with no row and no recovery.
    # CIP pass still requires name + address verified, so a missing address fails honestly.
    dob: Optional[str] = None
    ssn: Optional[str] = None
    address: Optional[str] = None
    entity_type: Optional[str] = None


class CipCheckOut(BaseModel):
    check_id: int
    application_id: int
    status: str  # "pass" | "fail"
    cip_passed: bool
    # CIP only. These two are hardcoded false to keep the gap visible (debt D11):
    # the service performs NO sanctions/OFAC screening and captures NO beneficial owner.
    sanctions_screened: bool = False
    ubo_captured: bool = False
    notes: str
