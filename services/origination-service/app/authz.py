"""ADR 0010 — officer-OR-owner authorization for application-scoped routes.

The gateway resolves the session and forwards X-User-Id (= users.id) and X-User-Role,
stripping any client-supplied copies (gateway _proxy), so origination can trust them.
Ownership is derived from data that already exists: a borrower login carries
users.applicant_id, and an application carries applications.applicant_id, so the user
who owns an application is the one whose users.applicant_id matches it. No new column and
no migration are needed.

Policy:
  - Officer roles (underwriter/admin) may act on ANY application.
  - A borrower may act ONLY on their own application (applicant_id match).
  - Everyone else -- including an anonymous /los caller with no X-User-Id -- is denied.

Fails closed. A non-officer who is not the owner is denied as 404, never a 403-on-exists:
the IDOR being closed is anonymous serial-id enumeration of applications, so the response
must not let a caller tell a real application id from a missing one.
"""

import hmac

from fastapi import HTTPException

from . import db

_OFFICER_ROLES = {"underwriter", "admin"}


def _is_officer(x_user_role: str | None) -> bool:
    return (x_user_role or "").strip().lower() in _OFFICER_ROLES


def _as_int(value: str | None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def require_officer(x_user_role: str | None) -> None:
    """Restrict a route to authenticated officer roles (assistant, application list)."""
    if not _is_officer(x_user_role):
        raise HTTPException(status_code=403, detail="officer role required")


def require_officer_or_owner(
    app_id: int,
    x_user_role: str | None,
    x_user_id: str | None,
    x_application_token: str | None = None,
) -> None:
    """Authorize an application-scoped action for an officer, the owning borrower, or an
    anonymous applicant holding this application's continuation token (ADR 0010 Phase B).

    The continuation token is the scoped capability issued at submit: it authorizes the
    logged-out applicant to complete decision/offer/accept on THIS application only, so
    anonymous apply keeps working without a login while serial-id enumeration stays closed
    (the token, not the guessable id, is the authorization).
    """
    if _is_officer(x_user_role):
        return  # officers act on any application; the route handles a genuine 404
    # Non-officer: allowed only as the owning borrower OR with this application's token.
    # A pure anonymous caller with neither a session nor a token is denied without any DB
    # lookup (no existence oracle) -- preserving the round-A short-circuit.
    user_id = _as_int(x_user_id)
    if user_id is None and not x_application_token:
        raise HTTPException(status_code=404, detail="application not found")
    app_rows = db.query(
        "SELECT applicant_id, continuation_token FROM applications WHERE id = %s",
        (app_id,),
    )
    app_row = app_rows[0] if app_rows else None
    if app_row is not None:
        # Owner: the caller's users.applicant_id matches the application's applicant_id.
        if user_id is not None:
            user_rows = db.query(
                "SELECT applicant_id FROM users WHERE id = %s", (user_id,)
            )
            caller_applicant_id = user_rows[0]["applicant_id"] if user_rows else None
            if (
                caller_applicant_id is not None
                and app_row["applicant_id"] == caller_applicant_id
            ):
                return
        # Continuation token: constant-time match against THIS application's stored token
        # (scoped to one app_id, so a token for app A cannot authorize app B). A legacy
        # row with a NULL token has no token path.
        stored = app_row["continuation_token"]
        if (
            x_application_token
            and stored
            and hmac.compare_digest(x_application_token, stored)
        ):
            return
    # Non-owner, wrong/absent token, or unknown application: deny without revealing
    # existence.
    raise HTTPException(status_code=404, detail="application not found")
