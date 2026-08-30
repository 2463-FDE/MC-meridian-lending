"""Session auth for the gateway.

Real-ish login: credentials are checked against the `users` table, a random opaque
token is minted and the session (user id / role / name) is stored in Redis with a TTL.
Subsequent requests present `Authorization: Bearer <token>`; the gateway resolves the
session and forwards the resolved identity downstream as `X-User-*` headers.

Password hashing (D27): salted PBKDF2-HMAC-SHA256, `_PBKDF2_ITERATIONS` per OWASP's
2023 minimum. A row still holding the pre-fix unsalted sha256(password) hex verifies
against `_legacy_hash` and is transparently rehashed to the new format on that
successful login (`authenticate`'s rehash branch) -- accounts drain to the new format
without a forced reset. `hash_password` never mints a legacy hash.

Caveats kept on purpose (brownfield): tokens never rotate, and downstream services
trust the forwarded `X-User-Role` without re-checking it.
"""

import hashlib
import hmac
import json
import os
import uuid

import redis

from . import db
from .config import REDIS_URL, RESUME_TTL_SECONDS, SESSION_TTL_SECONDS

_redis = None

_PBKDF2_ALGO = "pbkdf2_sha256"
_PBKDF2_ITERATIONS = 600_000
_SALT_BYTES = 16


def _client() -> "redis.Redis":
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Salted PBKDF2-HMAC-SHA256. Format: pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>."""
    if salt is None:
        salt = os.urandom(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return f"{_PBKDF2_ALGO}${_PBKDF2_ITERATIONS}${salt.hex()}${derived.hex()}"


def _legacy_hash(password: str) -> str:
    """Pre-D27 unsalted single-round sha256(password) -- verify-only, never minted."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _verify_password(password: str, stored_hash: str) -> bool:
    if stored_hash.startswith(f"{_PBKDF2_ALGO}$"):
        try:
            _, iterations_s, salt_hex, hash_hex = stored_hash.split("$", 3)
            salt = bytes.fromhex(salt_hex)
            iterations = int(iterations_s)
        except (ValueError, TypeError):
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(candidate.hex(), hash_hex)
    # Legacy unsalted row (D27): verify against the old scheme so existing accounts
    # keep authenticating; authenticate() rehashes on a successful legacy match.
    return hmac.compare_digest(_legacy_hash(password), stored_hash)


def authenticate(username: str, password: str) -> dict | None:
    rows = db.query(
        "SELECT id, username, role, display_name, password_hash, is_active "
        "FROM users WHERE username = %s",
        (username,),
    )
    if not rows:
        return None
    user = rows[0]
    if not user["is_active"]:
        return None
    if not _verify_password(password, user["password_hash"]):
        return None
    if not user["password_hash"].startswith(f"{_PBKDF2_ALGO}$"):
        db.query(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (hash_password(password), user["id"]),
        )
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "name": user["display_name"],
    }


def create_session(user: dict) -> str:
    token = uuid.uuid4().hex
    _client().setex(f"session:{token}", SESSION_TTL_SECONDS, json.dumps(user))
    return token


def get_session(token: str) -> dict | None:
    if not token:
        return None
    raw = _client().get(f"session:{token}")
    return json.loads(raw) if raw else None


def delete_session(token: str) -> None:
    if token:
        _client().delete(f"session:{token}")


# --- anonymous resume session (ADR 0010 Phase B, PR #7 review) ----------------
# The continuation token authorizes an anonymous applicant's own decision/offer/accept.
# Rather than return it to the browser (localStorage would expose it to any same-origin
# script / XSS), the gateway stashes it in Redis under an opaque session id and gives the
# browser only that id in an HttpOnly cookie. The raw token never leaves the server after
# issuance; the gateway re-attaches it as X-Application-Token when proxying to origination.


def create_resume_session(app_id, token: str) -> str:
    """Store {app_id, token} server-side; return an opaque session id for the cookie."""
    sid = uuid.uuid4().hex
    _client().setex(
        f"resume:{sid}",
        RESUME_TTL_SECONDS,
        json.dumps({"app_id": app_id, "token": token}),
    )
    return sid


def resolve_resume(sid: str | None) -> dict | None:
    """Return {app_id, token} for a resume session id, or None if unknown/expired."""
    if not sid:
        return None
    raw = _client().get(f"resume:{sid}")
    return json.loads(raw) if raw else None


def clear_resume(sid: str | None) -> None:
    """Revoke a resume session server-side (called when the application is accepted)."""
    if sid:
        _client().delete(f"resume:{sid}")


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return authorization.strip()
