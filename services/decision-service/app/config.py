"""Decision service configuration.

Carried over from origination when decisioning was split into its own service.
Bureau/DB credentials are now read from the environment only — no secret defaults
in source (was: inline "so the demo just works"). Inject via the host env /
secret manager; see docs/security-remediation-2026-07.md.
"""

import os
import threading
import time
from urllib.parse import unquote, urlparse

import psycopg2

# --- Credit bureau (Experian) — env only; no committed default. Rotate the key
# that was previously hardcoded/committed. ---
EXPERIAN_KEY = os.getenv("EXPERIAN_KEY", "")
EXPERIAN_BASE_URL = os.getenv(
    "EXPERIAN_BASE_URL", "https://api.experian.example.com/v2"
)

# Shared secret proving a request is an internal service-to-service call (origination's
# assistant reading the decision record), NOT an external caller coming through the
# anonymous gateway /decision proxy. Env only, no committed default (same posture as the
# bureau key). When unset the guard fails CLOSED — an unconfigured token never means open.
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")

# Keyed pepper for the SSN idempotency fingerprint (see decision._ssn_fingerprint).
# SSN drives the bureau pull, so a reused request_id arriving with a DIFFERENT SSN must
# not replay the recorded decision (PR review). We persist only an HMAC-SHA256 of the
# SSN (never the SSN itself — identifier-free record, ADR 0007). Env only; NO committed
# default (same posture as INTERNAL_SERVICE_TOKEN / EXPERIAN_KEY).
#
# CRITICAL: the digest is only non-reversible while the pepper is SECRET. An SSN is a
# 9-digit space, so a public/placeholder pepper lets anyone with decision_events access
# brute-force the fingerprints and recover SSNs (PR review). So a blank or KNOWN
# PLACEHOLDER pepper is treated as NO pepper — no fingerprint is ever persisted under it
# (see fingerprint_pepper) — and in a non-development deployment it reports unhealthy
# (missing_required_secrets) rather than silently keying a reversible digest.
DECISION_FINGERPRINT_PEPPER = os.getenv("DECISION_FINGERPRINT_PEPPER", "")

# Pepper values that must NEVER key a real fingerprint: blank, the value the template
# once shipped, and generic stubs. Compared case-insensitively; a copied .env.example
# can therefore never produce a reversible digest.
_PLACEHOLDER_PEPPERS = frozenset(
    {
        "",
        "demo-decision-fingerprint-pepper-change-me",
        "replace_with_fingerprint_pepper",
        "change-me",
        "changeme",
        "change_me",
        "placeholder",
    }
)


def fingerprint_pepper() -> str | None:
    """The SSN-fingerprint pepper, or None when it is unset or a known placeholder.

    A placeholder is treated as unset so a copied .env.example (or an operator who never
    set a real secret) can never key a reversible fingerprint. When this returns None the
    SSN-change conflict check degrades to the financial-input fields only; no
    ssn_fingerprint is persisted."""
    pepper = DECISION_FINGERPRINT_PEPPER.strip()
    if pepper.lower() in _PLACEHOLDER_PEPPERS:
        return None
    return pepper


# Deployment environment. Synthetic credit is gated on this being exactly
# "development", so no production config can enable it — not even by mistake.
ENVIRONMENT = os.getenv("ENVIRONMENT", "production").strip().lower()

# Local/demo escape hatch. When enabled, a missing EXPERIAN_KEY or a bureau
# failure falls back to a deterministic SYNTHETIC credit score so the stack runs
# without a live bureau. Guarded by TWO independent conditions (see
# synthetic_credit_enabled): the explicit opt-in flag AND ENVIRONMENT=development.
ALLOW_SYNTHETIC_CREDIT = os.getenv("ALLOW_SYNTHETIC_CREDIT", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def synthetic_credit_enabled() -> bool:
    """True only when synthetic scoring is BOTH explicitly opted into AND running
    in a development environment.

    Two independent gates so a production deployment can never issue decisions off
    a fake score by accident: ALLOW_SYNTHETIC_CREDIT alone does nothing unless
    ENVIRONMENT=development, and a dev environment does nothing without the flag.
    """
    return ENVIRONMENT == "development" and ALLOW_SYNTHETIC_CREDIT


def database_url_configured() -> bool:
    """True only when DATABASE_URL is set with a real, consistent password.

    This stack authenticates to Postgres with a password: docker-compose sets
    POSTGRES_PASSWORD via ${VAR:?} and .env.example embeds it in the DSN. A
    non-empty password is necessary but NOT sufficient: the template ships a
    REPLACE_WITH_POSTGRES_PASSWORD placeholder, and an operator can set
    POSTGRES_PASSWORD but leave the DSN on that placeholder or a stale value —
    /health would read healthy while the first real query fails auth (and
    decide() would swallow the persistence failure). So this also rejects known
    placeholder/stub tokens and, when POSTGRES_PASSWORD is present as the source
    of truth, requires the DSN password to match it, catching placeholder/stale
    drift without a DB round trip.

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
    """Secrets/config that MUST be present for a healthy runtime.

    Used by /health so a misconfigured deployment reports unhealthy instead of
    looking OK: no bureau key (in a non-synthetic config) means decisions off a
    stub score; an unset or passwordless DATABASE_URL means decisions that
    cannot be durably recorded.
    """
    missing = []
    if not synthetic_credit_enabled() and not EXPERIAN_KEY:
        missing.append("EXPERIAN_KEY")
    if not database_url_configured():
        missing.append("DATABASE_URL")
    # Fail loud on a missing internal-service token (PR review): without it the
    # internal-only routes (POST /decisions, GET record) fail closed, so a misconfig
    # would look healthy while every officer/assistant decision 503s. Surface at /health.
    if not INTERNAL_SERVICE_TOKEN:
        missing.append("INTERNAL_SERVICE_TOKEN")
    # Fail loud outside development when the SSN-fingerprint pepper is unset or still a
    # placeholder (PR review): a placeholder pepper would key a REVERSIBLE HMAC of the
    # SSN into the append-only decision_events (breaking the identifier-free guarantee),
    # and an unset one silently drops the reused-key SSN-change conflict check. A
    # development deployment may run without it (fingerprint simply not persisted).
    if ENVIRONMENT != "development" and fingerprint_pepper() is None:
        missing.append("DECISION_FINGERPRINT_PEPPER")
    return missing


# Core banking key — env only; no committed default.
CORE_BANKING_API_KEY = os.getenv("CORE_BANKING_API_KEY", "")

# No committed default: a passwordless fallback DSN (meridian:@postgres) would
# let a deploy that omits DATABASE_URL connect unauthenticated and look healthy.
# Unset/passwordless is reported unhealthy via missing_required_secrets().
DATABASE_URL = os.getenv("DATABASE_URL", "")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
