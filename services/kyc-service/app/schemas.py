"""Pydantic request/response models for the KYC API."""
from typing import Optional

from pydantic import BaseModel


class CipCheckIn(BaseModel):
    application_id: int
    applicant_id: int
    name: str
    dob: str
    ssn: str
    address: str
    entity_type: Optional[str] = None


class CipCheckOut(BaseModel):
    check_id: int
    application_id: int
    status: str          # "pass" | "fail"
    cip_passed: bool
    # CIP only. These two are hardcoded false to keep the gap visible (debt D11):
    # the service performs NO sanctions/OFAC screening and captures NO beneficial owner.
    sanctions_screened: bool = False
    ubo_captured: bool = False
    notes: str
