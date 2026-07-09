"""KYC API: CIP-only verification + persistence.

CIP only — no sanctions / OFAC screening, no beneficial-owner (UBO) capture, no ongoing
monitoring, no SAR path (debt D11). The kyc_checks write below mirrors how origination
persisted the row: raw psycopg2 INSERT, only the four CIP boolean columns (there are no
sanctions/ubo columns to persist — debt preserved).
"""
from fastapi import APIRouter

from .. import db, kyc
from ..logging_config import get_logger
from ..schemas import CipCheckIn, CipCheckOut

log = get_logger("kyc-api")
router = APIRouter(prefix="/kyc", tags=["kyc"])


@router.post("/check", response_model=CipCheckOut)
def kyc_check(body: CipCheckIn):
    payload = body.model_dump()
    # Allowlist log: identifiers only, never the raw payload. Dumping the full
    # request put client free text (name/dob/ssn/address) into the log, where a
    # PAN could hide behind separators the whole-line redactor can't fully catch.
    # (closes D5 on this path; redactor remains a backstop.)
    log.info("POST /kyc/check application_id=%s applicant_id=%s",
             payload.get("application_id"), payload.get("applicant_id"))
    cip = kyc.run_cip(payload)  # CIP only — no sanctions / UBO / monitoring (debt D11)

    # CIP "passes" if name + address verified. Entity applicants (no dob/ssn) still pass —
    # an LLC clears with no real person verified, and no UBO captured. (debt D11)
    cip_passed = bool(cip["name_verified"] and cip["address_verified"])
    status = "pass" if cip_passed else "fail"

    # persist the CIP result (still no sanctions/ubo columns to persist — debt preserved).
    # Raw psycopg2 write path, matching how origination wrote it.
    check_id = -1
    try:
        rows = db.query(
            "INSERT INTO kyc_checks (applicant_id, name_verified, dob_verified, "
            "address_verified, ssn_verified) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (body.applicant_id, cip["name_verified"], cip["dob_verified"],
             cip["address_verified"], cip["ssn_verified"]),
        )
        check_id = rows[0]["id"] if rows else -1
    except Exception as e:  # noqa
        log.warning("could not persist kyc: %s", e)

    return CipCheckOut(
        check_id=check_id,
        application_id=body.application_id,
        status=status,
        cip_passed=cip_passed,
        sanctions_screened=False,  # no OFAC/sanctions screening (debt D11)
        ubo_captured=False,        # no beneficial-owner capture (debt D11)
        notes="CIP only; no sanctions/OFAC, no UBO, no ongoing monitoring, no SAR path.",
    )
