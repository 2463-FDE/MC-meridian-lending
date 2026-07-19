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


def _verify_keys() -> list[tuple[str, str]]:
    """(version, secret) pairs accepted when VERIFYING a presented token (PR #7 review).

    The dedicated peppers from CONTINUATION_TOKEN_KEYS (newest first), PLUS a legacy fallback
    (INTERNAL_SERVICE_TOKEN under a stable "legacy" version) so tokens hashed BEFORE the
    dedicated pepper existed still verify until they expire. This fallback is verify-only:
    hash_token never issues a new token under the service secret (see hash_token), so this
    reads pre-existing rows, it does not perpetuate the coupling for new tokens."""
    keys = list(config.continuation_token_keys())
    if config.INTERNAL_SERVICE_TOKEN:
        keys.append(("legacy", config.INTERNAL_SERVICE_TOKEN))
    return keys


def _digest(raw: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def hash_token(raw: str) -> str:
    """Version-tagged keyed digest of a continuation token: "<version>:<hexdigest>".

    The token is a bearer credential for money-moving routes, so it is never stored in the
    clear: applications.continuation_token holds this digest, not the raw token, which is
    returned to the applicant exactly once at submit. A DB read / backup / logged row then
    yields only a non-replayable digest.

    Hashed with the CURRENT dedicated pepper (config.continuation_token_keys()[0]); the version
    prefix lets verify_token pick the right key after a rotation. NEW tokens are NEVER hashed
    with INTERNAL_SERVICE_TOKEN (PR #7 review): if no dedicated pepper is configured this
    refuses to issue outside development, so a production deploy cannot silently couple resume
    tokens to the service-auth secret. In development only, it falls back to the service token
    for local-demo convenience (missing_required_secrets already reports the missing pepper)."""
    keys = config.continuation_token_keys()
    if not keys:
        if config.ENVIRONMENT == "development" and config.INTERNAL_SERVICE_TOKEN:
            keys = [("legacy", config.INTERNAL_SERVICE_TOKEN)]
        else:
            raise RuntimeError(
                "CONTINUATION_TOKEN_KEYS is not configured; refusing to issue a continuation "
                "token hashed with the service-auth secret (PR #7 review). Set a dedicated "
                "pepper (version:secret) -- /health already reports this missing."
            )
    ver, secret = keys[0]
    return f"{ver}:{_digest(raw, secret)}"


def verify_token(raw: str, stored: str) -> bool:
    """Constant-time verify of a presented token against a stored version-tagged digest.

    Selects the key matching the stored version, so a rotation (new current key, old key kept
    configured) still verifies pre-rotation tokens until they expire. A legacy digest with no
    version prefix (pre-versioning) is tried against every accepted key. The accepted set
    (_verify_keys) includes the service-token legacy fallback so pre-pepper rows still verify."""
    if not raw or not stored:
        return False
    ver, sep, digest = stored.partition(":")
    if sep:
        candidates = [s for v, s in _verify_keys() if v == ver]
        target = digest
    else:
        # Unversioned legacy digest: try all accepted keys.
        candidates = [s for _, s in _verify_keys()]
        target = stored
    for secret in candidates:
        if hmac.compare_digest(
            _digest(raw, secret).encode("utf-8"), target.encode("utf-8")
        ):
            return True
    return False


def terminal_accept_replay(app_id: int, x_application_token: str | None) -> int | None:
    """Replay-only authorization for a retried anonymous accept after funding (PR review).

    The first accept funds the loan and retires the continuation token by nulling its
    expiry (accept_offer), so require_officer_or_owner now denies the retry (expired). But
    the token hash is preserved, and the applicant's lost-response retry still carries the
    raw token -- their only credential. Return the already-boarded loan id when that token
    verifies against THIS funded application's preserved hash and a loan exists; else None.

    This grants NO forward capability: it is called only by accept, requires the
    application to already be 'funded', and only ever RETURNS an existing loan (it never
    boards, mutates, or authorizes another route). So the retired token cannot re-drive a
    funded application -- it can only recover the idempotent success it already earned.
    """
    if not x_application_token:
        return None
    rows = db.query(
        "SELECT continuation_token FROM applications "
        "WHERE id = %s AND status = 'funded'",
        (app_id,),
    )
    if not rows:
        return None
    stored = rows[0]["continuation_token"]
    if not stored or not verify_token(x_application_token, stored):
        return None
    loans = db.query(
        "SELECT id FROM loans WHERE app_id = %s ORDER BY id LIMIT 1", (app_id,)
    )
    return loans[0]["id"] if loans else None


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
        # Continuation token: constant-time verify of the presented token against THIS
        # application's stored version-tagged digest (scoped to one app_id, so a token for
        # app A cannot authorize app B). The stored value is hash_token(raw), never the raw
        # token; verify_token selects the key matching the stored version, so a pepper
        # rotation keeps pre-rotation tokens working until they expire. A legacy/officer row
        # with a NULL token, or a token past its expiry, has no token path. verify_token
        # hashes the presented token before compare, so a non-ASCII X-Application-Token can
        # never reach compare_digest as a raw non-ASCII str -- no TypeError->500 oracle.
        stored = app_row["continuation_token"]
        expires_at = app_row["continuation_token_expires_at"]
        if (
            x_application_token
            and stored
            and not _expired(expires_at)
            and verify_token(x_application_token, stored)
        ):
            return
    # Non-owner, wrong/absent token, or unknown application: deny without revealing
    # existence.
    raise HTTPException(status_code=404, detail="application not found")
