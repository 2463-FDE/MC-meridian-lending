"""KYC — Customer Identification Program (CIP) only.

Verifies name / DOB / address / SSN against the bureau, then stops.

MISSING (deliberately, for now):
  - OFAC / sanctions screening
  - beneficial-owner (UBO) identification for entity applicants
  - ongoing monitoring
  - SAR path
An LLC can clear this with no real person verified.

This logic used to live inside origination-service (app/kyc.py); it is now its own
kyc-service. Splitting it into a standalone service did NOT close the gap — the CIP-only
story (no sanctions/OFAC, no UBO, no ongoing monitoring, no SAR) is carried forward intact.
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
    # Allowlist log: applicant_id + the boolean CIP result only. The raw name is
    # client-controlled free text (could hide a PAN) and is PII in its own right,
    # so it is never logged. redactor stays a backstop for the id/result string.
    log.info("CIP check applicant_id=%s result=%s",
             applicant.get("applicant_id"), result)
    return result
