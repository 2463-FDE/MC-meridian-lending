"""Session auth for the gateway.

Real-ish login: credentials are checked against the `users` table, a random opaque
token is minted and the session (user id / role / name) is stored in Redis with a TTL.
Subsequent requests present `Authorization: Bearer <token>`; the gateway resolves the
session and forwards the resolved identity downstream as `X-User-*` headers.

Caveats kept on purpose (brownfield): password hashes are unsalted sha256, tokens never
rotate, and downstream services trust the forwarded `X-User-Role` without re-checking it.
"""

import hashlib
import json
import uuid

import redis

from . import db
from .config import REDIS_URL, RESUME_TTL_SECONDS, SESSION_TTL_SECONDS

_redis = None


def _client() -> "redis.Redis":
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


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
    if user["password_hash"] != hash_password(password):
        return None
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
