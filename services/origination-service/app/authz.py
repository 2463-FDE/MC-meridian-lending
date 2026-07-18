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

import hashlib
import hmac
from datetime import datetime, timezone

from fastapi import HTTPException

from . import config, db

_OFFICER_ROLES = {"underwriter", "admin"}


def _expired(expires_at: datetime | None) -> bool:
    """A continuation token with no expiry (legacy/officer rows never had one) or a past
    expiry is not a valid token capability. Fail closed: treat a missing expiry as expired
    on the token path, so only a freshly issued, unexpired token authorizes."""
    if expires_at is None:
        return True
    if expires_at.tzinfo is None:  # DB may return a naive UTC timestamp
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires_at


def hash_token(raw: str) -> str:
    """Keyed hash of a continuation token for storage/compare (PR #7 review).

    The token is a bearer credential for money-moving routes, so it is never stored in
    the clear: applications.continuation_token holds this hex digest, not the raw token,
    which is returned to the applicant exactly once at submit. A DB read / backup leak /
    logged row then yields only the digest, which cannot be replayed as X-Application-Token.
    Keyed with INTERNAL_SERVICE_TOKEN (a server-only secret) so a DB-without-key compromise
    cannot even offline-verify guesses; unkeyed in dev when the secret is unset (the raw
    token is already 256-bit random, so at-rest confidentiality holds either way).
    """
    return hmac.new(
        config.INTERNAL_SERVICE_TOKEN.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


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
        "SELECT applicant_id, continuation_token, continuation_token_expires_at "
        "FROM applications WHERE id = %s",
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
        # Continuation token: constant-time match of the KEYED HASH of the presented token
        # against THIS application's stored digest (scoped to one app_id, so a token for
        # app A cannot authorize app B). The stored value is hash_token(raw), never the raw
        # token. A legacy/officer row with a NULL token, or a token past its expiry, has no
        # token path. Compare as UTF-8 bytes (the digest is hex/ASCII, but hashing the
        # presented token first also means a non-ASCII X-Application-Token can never reach
        # compare_digest as a raw non-ASCII str -- no TypeError->500 existence oracle).
        stored = app_row["continuation_token"]
        expires_at = app_row["continuation_token_expires_at"]
        if (
            x_application_token
            and stored
            and not _expired(expires_at)
            and hmac.compare_digest(
                hash_token(x_application_token).encode("utf-8"),
                stored.encode("utf-8"),
            )
        ):
            return
    # Non-owner, wrong/absent token, or unknown application: deny without revealing
    # existence.
    raise HTTPException(status_code=404, detail="application not found")
