"""CIP tests for the KYC service (these PASS).

NOTE (deliberate coverage gap): there is intentionally NO test for sanctions/OFAC
screening, beneficial-owner (UBO) capture, ongoing monitoring, or entity-applicant
screening. Those paths don't exist (debt D11) and the debt area stays untested — an
entity/LLC clearing CIP with no real person verified is exactly the gap left unguarded.
"""
from app.kyc import run_cip


def test_individual_applicant_passes_cip():
    applicant = {
        "name": "Jane Borrower",
        "dob": "1990-04-12",
        "ssn": "123-45-6789",
        "address": "42 Main St, Springfield",
    }
    result = run_cip(applicant)
    assert result["name_verified"] is True
    assert result["dob_verified"] is True
    assert result["address_verified"] is True
    assert result["ssn_verified"] is True
