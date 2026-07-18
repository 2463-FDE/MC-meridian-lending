"""Gateway configuration."""

import os
import threading
import time
from urllib.parse import unquote, urlparse

import psycopg2
import redis

# No committed default: a passwordless fallback DSN (meridian:@postgres) would
# let a deploy that omits DATABASE_URL connect unauthenticated and look healthy.
# Unset/passwordless is reported unhealthy via missing_required_secrets().
DATABASE_URL = os.getenv("DATABASE_URL", "")


def database_url_configured() -> bool:
    """True only when DATABASE_URL is set with a real, consistent password.

    Password auth is how this stack reaches Postgres (compose sets
    POSTGRES_PASSWORD via ${VAR:?}; .env.example embeds it in the DSN). A
    non-empty password is necessary but NOT sufficient: the template ships a
    REPLACE_WITH_POSTGRES_PASSWORD placeholder, and an operator can set
    POSTGRES_PASSWORD but leave the DSN on that placeholder or a stale value —
    /health would read healthy while the first real query fails auth. So this
    also rejects known placeholder/stub tokens and, when POSTGRES_PASSWORD is
    present as the source of truth, requires the DSN password to match it,
    catching placeholder/stale drift without a DB round trip.

    Residual (documented): this proves the password is real and consistent, not
    that it authenticates. A wrong password with no POSTGRES_PASSWORD to compare
    (e.g. an external managed DB whose secret lives only in the DSN) is caught by
    database_reachable(), the bounded live probe /health runs after this gate.
    Passwordless auth (IAM/peer/PGPASSWORD)
    must revisit this gate — here, passwordless means misconfigured.
    """
    if not DATABASE_URL:
        return False
    try:
        password = urlparse(DATABASE_URL).password
    except ValueError:
        return False
    if not password:
        return False
    # urlparse returns the percent-ENCODED password; decode it so a reserved-char
    # password (p@ss -> p%40ss in the DSN) is not falsely flagged as a placeholder
    # or as drifted from raw POSTGRES_PASSWORD.
    password = unquote(password)
    # Known placeholder passwords are never valid. The previously-committed
    # credential is intentionally NOT listed here — embedding that literal would
    # re-commit the leaked secret in every clone/image and defeat the purge. A
    # stale/rotated DSN is caught by the POSTGRES_PASSWORD consistency check below.
    if password.lower() in {
        "replace_with_postgres_password",
        "changeme",
        "change_me",
        "password",
        "postgres",
    }:
        return False
    # When POSTGRES_PASSWORD is the source of truth (compose ${VAR:?}), the DSN
    # password must match it — catches a stale/placeholder DSN without a DB call.
    expected = os.getenv("POSTGRES_PASSWORD")
    if expected and password != expected:
        return False
    return True


# Short-TTL cache of the last probe, keyed on the DSN. /health is unauthenticated
# and hit by load balancers (and anyone on the published port); without this each
# request would open a new, unpooled Postgres connection, so a flood could exhaust
# max_connections or the sync threadpool. Collapsing bursts to one probe per TTL
# removes that amplifier. Cost: /health can lag a DB up/down transition by up to
# the TTL — acceptable for readiness (the healthcheck interval is longer). Stored
# as a single tuple so a concurrent read never sees torn state under the GIL.
_PROBE_TTL_SECONDS = 5.0
_probe_state = (None, 0.0, (False, None))  # (dsn, monotonic_at, result)
# Single-flight: only one thread probes per DSN/TTL window; concurrent misses wait
# on this and reuse the fresh result instead of each opening its own connection.
_probe_lock = threading.Lock()


def reset_database_probe_cache() -> None:
    """Drop the cached probe result (forces the next call to reconnect)."""
    global _probe_state
    _probe_state = (None, 0.0, (False, None))


def database_reachable(timeout: float = 2.0) -> tuple[bool, str | None]:
    """Bounded live probe (TTL-cached): open a Postgres connection, run SELECT 1.

    database_url_configured() only proves the DSN password is non-placeholder and
    (when POSTGRES_PASSWORD is set) matches it — it does NOT prove the password
    authenticates. This closes that documented residual: a wrong password with no
    POSTGRES_PASSWORD to compare against (e.g. an external managed DB whose secret
    lives only in the DSN) is caught only by actually connecting. connect_timeout
    and a server-side statement_timeout bound the probe so /health cannot hang; the
    result is cached for _PROBE_TTL_SECONDS so a flood of /health calls cannot open
    a Postgres connection per request.

    Returns (ok, error); error is the exception class name only — never the DSN or
    its password — so /health cannot leak credentials.
    """
    global _probe_state
    dsn, at, result = _probe_state
    if dsn == DATABASE_URL and (time.monotonic() - at) < _PROBE_TTL_SECONDS:
        return result
    # Cold cache or expired TTL: single-flight so a burst of concurrent misses
    # (e.g. an unauthenticated /health flood) performs ONE probe, not one Postgres
    # connection per request. The check above stays lock-free for the warm path;
    # only a miss contends on the lock, and the re-check inside lets every caller
    # after the winner reuse the fresh result.
    with _probe_lock:
        dsn, at, result = _probe_state
        if dsn == DATABASE_URL and (time.monotonic() - at) < _PROBE_TTL_SECONDS:
            return result
        result = _run_database_probe(timeout)
        _probe_state = (DATABASE_URL, time.monotonic(), result)
        return result


def _run_database_probe(timeout: float) -> tuple[bool, str | None]:
    if not DATABASE_URL:
        return False, "DATABASE_URL not set"
    conn = None
    try:
        conn = psycopg2.connect(
            DATABASE_URL,
            connect_timeout=max(1, int(timeout)),
            options="-c statement_timeout=2000",
        )
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True, None
    except Exception as exc:
        return False, exc.__class__.__name__
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def missing_required_secrets() -> list:
    """Config that MUST be present for a healthy runtime; surfaced by /health so
    an unset or passwordless DATABASE_URL reports unhealthy instead of connecting
    unauthenticated (or failing opaquely at query time) while looking OK."""
    missing = []
    if not database_url_configured():
        missing.append("DATABASE_URL")
    return missing


REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")


# Gateway auth stores and reads sessions in Redis (see auth._client), so a Redis
# outage breaks login, /auth/me, /lss, and /payments even while Postgres is fine.
# /health probes Redis too, with the same bounded, TTL-cached, single-flight shape
# as the DB probe, so a Redis outage flips readiness to unhealthy instead of
# leaving the load balancer routing auth traffic to an instance that can't serve it.
_REDIS_PROBE_TTL_SECONDS = 5.0
_redis_probe_state = (None, 0.0, (False, None))  # (url, monotonic_at, result)
_redis_probe_lock = threading.Lock()


def reset_redis_probe_cache() -> None:
    """Drop the cached Redis probe result (forces the next call to reconnect)."""
    global _redis_probe_state
    _redis_probe_state = (None, 0.0, (False, None))


def redis_reachable(timeout: float = 2.0) -> tuple[bool, str | None]:
    """Bounded live Redis PING, TTL-cached and single-flight — same shape as
    database_reachable, so a /health flood cannot open a Redis connection per
    request. Returns (ok, error); error is the exception class name only, never
    the URL, so /health cannot leak connection details.
    """
    global _redis_probe_state
    url, at, result = _redis_probe_state
    if url == REDIS_URL and (time.monotonic() - at) < _REDIS_PROBE_TTL_SECONDS:
        return result
    with _redis_probe_lock:
        url, at, result = _redis_probe_state
        if url == REDIS_URL and (time.monotonic() - at) < _REDIS_PROBE_TTL_SECONDS:
            return result
        result = _run_redis_probe(timeout)
        _redis_probe_state = (REDIS_URL, time.monotonic(), result)
        return result


def _run_redis_probe(timeout: float) -> tuple[bool, str | None]:
    client = None
    try:
        client = redis.Redis.from_url(
            REDIS_URL,
            socket_connect_timeout=max(1, int(timeout)),
            socket_timeout=max(1, int(timeout)),
        )
        client.ping()
        return True, None
    except Exception as exc:
        return False, exc.__class__.__name__
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


ORIGINATION_URL = os.getenv("ORIGINATION_URL", "http://origination-service:8001")
SERVICING_URL = os.getenv("SERVICING_URL", "http://servicing-service:8002")
KYC_URL = os.getenv("KYC_URL", "http://kyc-service:8003")
DECISION_URL = os.getenv("DECISION_URL", "http://decision-service:8004")
DISCLOSURE_URL = os.getenv("DISCLOSURE_URL", "http://disclosure-service:8005")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment-service:8006")

# Shared service-to-service secret. The gateway strips any client-supplied X-Internal-Service
# (it must never be forgeable through the proxy) and normally does not originate internal
# calls, but it needs the token to invoke origination's internal /abandon compensator when a
# resume session cannot be stored after submit (PR #7 review). Same value as every service's
# INTERNAL_SERVICE_TOKEN (compose loads .env for all). Unset -> compensation is skipped
# (the inert application is left for officer reconciliation).
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")

# 8-hour sessions. (No refresh, no rotation, no CSRF token — Halcyon "v1 auth".)
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "28800"))

# Anonymous resume session (ADR 0010 Phase B, PR #7 review). The continuation token is a
# bearer credential for money-moving routes; instead of returning it to the browser (where
# localStorage would expose it to any same-origin script / XSS), the gateway keeps it
# server-side in Redis keyed by an opaque session id and hands the browser only an
# HttpOnly cookie holding that id. Default TTL mirrors origination's CONTINUATION_TOKEN_TTL.
RESUME_TTL_SECONDS = int(os.getenv("RESUME_TTL_SECONDS", str(7 * 24 * 3600)))

# The browser origin allowed to send credentialed (cookie-bearing) requests. Credentialed
# CORS forbids a "*" origin, so this must be the concrete portal origin.
PORTAL_ORIGIN = os.getenv("PORTAL_ORIGIN", "http://localhost:3000")

# Set the Secure flag on the resume cookie (https-only). Default on; set false for local
# http development so the cookie is still sent over http://localhost.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() == "true"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
