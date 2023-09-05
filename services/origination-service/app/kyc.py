"""KYC — Customer Identification Program (CIP) only.

Verifies name / DOB / address / SSN against the bureau, then stops.

MISSING (deliberately, for now):
  - OFAC / sanctions screening
  - beneficial-owner (UBO) identification for entity applicants
  - ongoing monitoring
  - SAR path
An LLC can clear this with no real person verified.
"""
from .logging_config import get_logger

log = get_logger("kyc")


def run_cip(applicant: dict) -> dict:
    """Return a CIP result. This is the entire KYC story today."""
    # "verification" is a stub — in the demo we just echo that the fields are present.
    result = {
        "name_verified": bool(applicant.get("name")),
        "dob_verified": bool(applicant.get("dob")),
        "address_verified": bool(applicant.get("address")),
        "ssn_verified": bool(applicant.get("ssn")),
    }
    # NOTE: entity applicants (LLC) have no dob/ssn — they still "pass" CIP here.
    log.info("CIP check applicant=%s result=%s", applicant.get("name"), result)
    return result
