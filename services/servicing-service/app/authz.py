"""ADR 0014 Decision 1 — authorization for servicing's money and read routes.

Closes debt D8(b). Before this, `servicing-service` had no authorization module at
all: `adjust_balance` and `waive_fee` declared `x_user_role` and never read it, and
every read was reachable by walking serial loan ids. Any authenticated caller —
including a borrower login, which Lending Ops confirms is on the public internet —
could move money on any account.

Mirrors the SHAPE of `services/origination-service/app/authz.py` (a role set, a raising
require_* gate, an owner check that denies as 404) with role sets of its own. The role
sets deliberately differ from origination's `_OFFICER_ROLES = {underwriter, admin}`:
a CSR is the role that services accounts, and an underwriter decides applications
rather than adjusting live balances. Both sets are the ones Lending Ops confirmed on
2026-08-12; changing them is a two-literal edit, not a redesign.

The gateway resolves the session and forwards X-User-Id (= users.id) and X-User-Role,
stripping any client-supplied copies (gateway `_proxy_raw`), so these headers are
authentic here. A caller reaching this service directly inside the cluster carries no
session, so an absent or unrecognized role is denied — the gates fail closed.

Ownership needs no new column and no identity programme, which is what ADR 0010
deferred servicing on. It derives from data that already exists:
`loans.app_id` -> `applications.applicant_id`, and a borrower login carries
`users.applicant_id`. A loan whose `app_id` is NULL (legacy rows the partial unique
index tolerates) has no derivable owner and is staff-only.

Denials on loan-scoped routes are 404, never 403-on-exists: the IDOR being closed is
serial-id enumeration, so the response must not let a caller tell a real loan id from
a missing one.
"""

import hmac

from fastapi import HTTPException

from . import config, db
from .logging_config import get_logger

log = get_logger("authz")

# May act on a balance at all: adjust-balance, waive-fee.
_MONEY_ROLES = {"csr", "admin"}
# May read any serviced loan. Broader than _MONEY_ROLES on purpose — an underwriter
# looking at a serviced loan is ordinary work; adjusting its balance is not.
_STAFF_ROLES = {"csr", "underwriter", "admin"}


def _normalized(x_user_role: str | None) -> str:
    return (x_user_role or "").strip().lower()


def _is_money_role(x_user_role: str | None) -> bool:
    return _normalized(x_user_role) in _MONEY_ROLES


def _is_staff(x_user_role: str | None) -> bool:
    return _normalized(x_user_role) in _STAFF_ROLES


def _as_int(value: str | None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def require_money_role(x_user_role: str | None) -> None:
    """Restrict a discretionary money move to the roles that service accounts.

    403 rather than 404: the route is account-scoped but the refusal is about the
    caller's role, not about whether the loan exists, and the loan id in the path was
    not confirmed either way. A borrower reaching this gets the same 403 as an
    underwriter.
    """
    if not _is_money_role(x_user_role):
        raise HTTPException(status_code=403, detail="servicing money role required")


def require_internal_caller(x_internal_service: str | None) -> None:
    """Gate a route to internal service-to-service callers.

    Mirrors the kyc/decision/disclosure guards: the gateway strips any client-supplied
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


def _owns_loan(loan_id: int, user_id: int) -> bool:
    """True when this user's applicant is the applicant on the loan's application.

    Two reads by indexed key, mirroring origination's owner check rather than a single
    join, so the same shape is recognizable across both services. A loan with no
    `app_id`, an application with no `applicant_id`, or a user with no `applicant_id`
    (every staff login) yields False — absence is never a match.
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


def require_staff_or_owner(
    loan_id: int, x_user_role: str | None, x_user_id: str | None
) -> None:
    """Authorize a loan-scoped read for staff or the owning borrower.

    Staff short-circuit without a DB read, so a staff caller pays nothing and a
    non-staff caller with no user id is refused without a lookup — no existence oracle
    from timing or from the response.
    """
    if _is_staff(x_user_role):
        return  # the route itself still 404s a loan that does not exist
    user_id = _as_int(x_user_id)
    if user_id is None or not _owns_loan(loan_id, user_id):
        raise HTTPException(status_code=404, detail="loan not found")


def require_staff(x_user_role: str | None) -> None:
    """Restrict a route that is not scoped to one loan, so ownership cannot narrow it.

    The portfolio list returns every borrower's name and balance, so there is no owner
    to fall back to: a borrower gets 403, not a filtered page. Filtering it to the
    caller's own loans would be a new product surface, not an authorization fix.
    """
    if not _is_staff(x_user_role):
        raise HTTPException(status_code=403, detail="servicing staff role required")
