"""ADR 0014 Decision 1 — authorization for payment-service's capture route.

Closes the front-door half of debt D8(b): servicing-service's OWN /payments route
already requires a money role or the owning borrower (services/servicing-service/app/
authz.py require_money_role_or_owner), but payment-service's POST /payments -- the
route the gateway and frontend actually call (services/payment-service/app/payments.py
docstring: "both services expose the same POST /payments charge path and must not
diverge") -- took only `body` and never checked loan ownership. Any authenticated
caller could capture a real charge against any loan id and ride the internal-service
token straight past servicing's gate as a confused deputy.

Kept as a same-shape copy of servicing-service's authz.py rather than a shared package,
mirroring how the PII redactor is duplicated per service in this codebase (no shared
package there either) -- lifting this into a shared library is a larger change than
closing the open route.

The gateway resolves the session and forwards X-User-Id (= users.id) and X-User-Role,
stripping any client-supplied copies (gateway `_proxy_raw`), so these headers are
authentic here. Denied as 404, matching servicing-service's own /payments: the IDOR
being closed is loan-id enumeration, so the response must not let a caller tell a real
loan id from a missing one.
"""

from fastapi import HTTPException

from . import db

# May charge a card at all without owning the loan: same set servicing-service trusts
# for its own money-moving writes (adjust-balance, waive-fee, its own /payments).
_MONEY_ROLES = {"csr", "admin"}


def _normalized(x_user_role: str | None) -> str:
    return (x_user_role or "").strip().lower()


def _is_money_role(x_user_role: str | None) -> bool:
    return _normalized(x_user_role) in _MONEY_ROLES


def _as_int(value: str | None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _owns_loan(loan_id: int, user_id: int) -> bool:
    """True when this user's applicant is the applicant on the loan's application.

    Same query shape as servicing-service's authz._owns_loan -- both services read
    the same shared Postgres schema (ADR 0002), so this needs no cross-service call.
    """
    loan_rows = db.query(
        "SELECT app.applicant_id AS applicant_id "
        "FROM loans l JOIN applications app ON app.id = l.app_id "
        "WHERE l.id = %s",
        (loan_id,),
    )
    loan_applicant_id = loan_rows[0]["applicant_id"] if loan_rows else None
    if loan_applicant_id is None:
        return False
    user_rows = db.query("SELECT applicant_id FROM users WHERE id = %s", (user_id,))
    caller_applicant_id = user_rows[0]["applicant_id"] if user_rows else None
    return caller_applicant_id is not None and caller_applicant_id == loan_applicant_id


def require_money_role_or_owner(
    loan_id: int, x_user_role: str | None, x_user_id: str | None
) -> None:
    """Authorize a charge (POST /payments) for a money role or the loan's owner.

    A charge is a money-moving write: an underwriter is deliberately NOT a money
    role, so it falls through to the ownership check like any other non-money
    caller -- and is denied, since a staff login carries no applicant_id.
    """
    if _is_money_role(x_user_role):
        return
    user_id = _as_int(x_user_id)
    if user_id is None or not _owns_loan(loan_id, user_id):
        raise HTTPException(status_code=404, detail="loan not found")
