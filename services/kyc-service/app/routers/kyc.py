"""KYC API: CIP-only verification + persistence.

CIP only — no sanctions / OFAC screening, no beneficial-owner (UBO) capture, no ongoing
monitoring, no SAR path (debt D11). The kyc_checks write below mirrors how origination
persisted the row: raw psycopg2 INSERT, only the four CIP boolean columns (there are no
sanctions/ubo columns to persist — debt preserved).
"""

import hmac

from fastapi import APIRouter, Header, HTTPException

from .. import config, db, kyc
from ..logging_config import get_logger
from ..schemas import CipCheckIn, CipCheckOut

log = get_logger("kyc-api")
router = APIRouter(prefix="/kyc", tags=["kyc"])


def _require_internal_caller(x_internal_service: str | None) -> None:
    """Gate a route to internal service-to-service callers (PR review).

    Mirrors the decision/origination guards: the gateway strips any client-supplied
    X-Internal-Service, so only a caller reaching this service directly with the shared
    secret is accepted. Fails closed when the token is unconfigured (503); constant-time
    byte compare so the token cannot be timed out or crash on a non-ASCII value.
    """
    expected = config.INTERNAL_SERVICE_TOKEN
    if not expected:
        log.error("INTERNAL_SERVICE_TOKEN not configured; refusing internal route")
        raise HTTPException(status_code=503, detail="internal auth not configured")
    if not x_internal_service or not hmac.compare_digest(
        x_internal_service.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=403, detail="internal service identity required"
        )


@router.post("/check", response_model=CipCheckOut)
def kyc_check(
    body: CipCheckIn,
    x_internal_service: str | None = Header(default=None, alias="X-Internal-Service"),
):
    # Internal-only (PR review): this persists a CIP identity-verification record
    # (kyc_checks) from caller-supplied name/dob/ssn/address and is reachable through
    # the gateway's anonymous /kyc proxy. Without this an external caller could forge a
    # verification result for any applicant. Only origination calls it (during intake),
    # forwarding the shared secret; the gateway strips any client-supplied copy.
    _require_internal_caller(x_internal_service)
    payload = body.model_dump()
    # Allowlist log: identifiers only, never the raw payload. Dumping the full
    # request put client free text (name/dob/ssn/address) into the log, where a
    # PAN could hide behind separators the whole-line redactor can't fully catch.
    # (closes D5 on this path; redactor remains a backstop.)
    log.info(
        "POST /kyc/check application_id=%s applicant_id=%s",
        payload.get("application_id"),
        payload.get("applicant_id"),
    )
    cip = kyc.run_cip(payload)  # CIP only — no sanctions / UBO / monitoring (debt D11)

    # CIP "passes" if name + address verified. Entity applicants (no dob/ssn) still pass —
    # an LLC clears with no real person verified, and no UBO captured. (debt D11)
    cip_passed = bool(cip["name_verified"] and cip["address_verified"])
    status = "pass" if cip_passed else "fail"

    # persist the CIP result (still no sanctions/ubo columns to persist — debt preserved).
    # Raw psycopg2 write path, matching how origination wrote it.
    #
    # Fail closed if the row cannot be written (ADR 0011, PR review): the persisted
    # kyc_checks row -- not this response -- is the gate for decision/offer/boarding
    # (origination require_kyc_passed). Returning status=pass with no row would tell the
    # applicant they were KYC-checked while every downstream action stays blocked, an
    # application with no in-product recovery path. A 503 surfaces the persistence failure
    # to the caller (origination records kyc_unavailable and flags kyc_checked=False)
    # instead of hiding it behind a successful KYC response.
    try:
        rows = db.query(
            "INSERT INTO kyc_checks (applicant_id, name_verified, dob_verified, "
            "address_verified, ssn_verified) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (
                body.applicant_id,
                cip["name_verified"],
                cip["dob_verified"],
                cip["address_verified"],
                cip["ssn_verified"],
            ),
        )
    except Exception as e:  # noqa
        log.error("could not persist kyc for applicant_id=%s: %s", body.applicant_id, e)
        raise HTTPException(
            status_code=503, detail="could not persist identity verification result"
        )
    if not rows:
        log.error("kyc insert returned no id for applicant_id=%s", body.applicant_id)
        raise HTTPException(
            status_code=503, detail="could not persist identity verification result"
        )
    check_id = rows[0]["id"]

    return CipCheckOut(
        check_id=check_id,
        application_id=body.application_id,
        status=status,
        cip_passed=cip_passed,
        sanctions_screened=False,  # no OFAC/sanctions screening (debt D11)
        ubo_captured=False,  # no beneficial-owner capture (debt D11)
        notes="CIP only; no sanctions/OFAC, no UBO, no ongoing monitoring, no SAR path.",
    )
