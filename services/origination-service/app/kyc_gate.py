"""ADR 0011: require a passing CIP/KYC check before an application advances to a
regulated / money action (decision, offer generation, acceptance/boarding).

Before this gate, KYC was advisory: submit recorded kyc_checked but nothing downstream
enforced it, so a logged-out applicant (holding their continuation token) or any caller
could decision, offer, and board a funded loan with a failed identity check -- or with no
check at all when kyc-service was down at submit. This gate closes that.

Pass definition is applicant-type aware (PR review). kyc-service's own cip_passed is
name + address only -- deliberately so an entity/LLC (which has no DOB/SSN and no real
person) can clear CIP (debt D11). But mirroring that rule for a NATURAL PERSON let a
natural-person application reach a regulated credit pull / boarding with DOB and SSN
never even asserted, now that ApplicationIn permits them null. So the gate requires:
  - natural person (is_entity false): name + DOB + address + SSN all verified;
  - entity (is_entity true): name + address verified (the documented D11 entity carve-out).
The authoritative source is the persisted kyc_checks row, not submit's response.

Scope of what this closes vs debt D11: this makes the REQUIRED identity ELEMENTS
type-correct, so a natural person cannot advance with no DOB/SSN on file. It does NOT
raise the verification DEPTH -- run_cip is still a stub that sets each *_verified from
mere field presence (bool(value)), so placeholder DOB/SSN strings still verify, and no
sanctions/OFAC or UBO screening exists. That depth gap is deliberate debt D11 (see
kyc-service/app/kyc.py), unchanged here; the gate cannot invent verification the service
does not perform.

Fails closed: an application whose applicant has a FAILING latest kyc_checks row (CIP
declined) OR no kyc_checks row at all (the check never ran -- e.g. kyc-service was
unavailable at submit) cannot advance. Raises 409 (state not satisfied), matching the
decision-state guards already in accept_offer / make_offer. Availability tradeoff
(accepted, ADR 0011): during a KYC outage an application can still be submitted but cannot
advance until a passing check exists; recovery today is re-submitting once KYC is back.
"""

from fastapi import HTTPException

from . import db


def require_kyc_passed(app_id: int) -> None:
    rows = db.query(
        "SELECT ap.is_entity, kc.name_verified, kc.address_verified, "
        "kc.dob_verified, kc.ssn_verified "
        "FROM applications a "
        "JOIN applicants ap ON ap.id = a.applicant_id "
        "LEFT JOIN kyc_checks kc ON kc.applicant_id = a.applicant_id "
        "WHERE a.id = %s ORDER BY kc.id DESC LIMIT 1",
        (app_id,),
    )
    # No row => application/applicant missing OR no kyc_checks yet (LEFT JOIN leaves the
    # kc.* columns NULL). Fail closed. A row with a required CIP boolean false/NULL =>
    # the identity check declined or never covered that element. Either way, block.
    if not rows:
        _block()
    row = rows[0]
    passed = bool(row["name_verified"]) and bool(row["address_verified"])
    if not row["is_entity"]:
        # Natural person: DOB + SSN are required identity elements (entities have none --
        # D11 carve-out). NULL from the LEFT JOIN (no kyc row) is falsy => blocks.
        passed = passed and bool(row["dob_verified"]) and bool(row["ssn_verified"])
    if not passed:
        _block()


def _block() -> None:
    raise HTTPException(
        status_code=409,
        detail="identity verification (KYC) has not passed for this application",
    )
