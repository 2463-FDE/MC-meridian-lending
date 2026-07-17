"""ADR 0011: require a passing CIP/KYC check before an application advances to a
regulated / money action (decision, offer generation, acceptance/boarding).

Before this gate, KYC was advisory: submit recorded kyc_checked but nothing downstream
enforced it, so a logged-out applicant (holding their continuation token) or any caller
could decision, offer, and board a funded loan with a failed identity check -- or with no
check at all when kyc-service was down at submit. This gate closes that.

Pass definition mirrors kyc-service's own (name + address verified -- see
kyc-service/app/routers/kyc.py::cip_passed); origination does not invent a stricter or
looser rule than the service that performs the check. The authoritative source is the
persisted kyc_checks row, not submit's response.

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
        "SELECT kc.name_verified, kc.address_verified "
        "FROM applications a "
        "LEFT JOIN kyc_checks kc ON kc.applicant_id = a.applicant_id "
        "WHERE a.id = %s ORDER BY kc.id DESC LIMIT 1",
        (app_id,),
    )
    # No row => application missing OR no kyc_checks yet (fail closed). A row with the
    # CIP booleans false => the identity check declined. Either way, block.
    if not rows or not (rows[0]["name_verified"] and rows[0]["address_verified"]):
        raise HTTPException(
            status_code=409,
            detail="identity verification (KYC) has not passed for this application",
        )
