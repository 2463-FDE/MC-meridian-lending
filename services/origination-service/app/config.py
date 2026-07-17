"""Origination service configuration.

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

# Core banking key — env only; no committed default.
CORE_BANKING_API_KEY = os.getenv("CORE_BANKING_API_KEY", "")

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
            # Schema readiness (PR review): decisioning reads applications.monthly_debt
            # (decision_request_payload). Migrations are hand-applied and lag the init
            # DDL (CLAUDE.md / ADR 0002), so a DB volume that predates 0006 is reachable
            # but the SELECT would 500 the decision path. Fail readiness loud here,
            # naming the missing column, so an unmigrated deployment shows at /health.
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'applications' AND column_name = 'monthly_debt'"
            )
            if cur.fetchone() is None:
                return False, "schema_not_ready:applications.monthly_debt"
            # Idempotent boarding depends on the uq_loans_app unique index (PR review):
            # it is what turns a concurrent duplicate acceptance into the UniqueViolation
            # accept_offer catches and replays. A partially-applied migration with the
            # loans table but no index would let concurrent accepts board duplicate loans
            # while /health reads healthy — same class as the decision idempotency index.
            cur.execute("SELECT 1 FROM pg_indexes WHERE indexname = 'uq_loans_app'")
            if cur.fetchone() is None:
                return False, "schema_not_ready:uq_loans_app"
            # ADR 0010 Phase B: the anonymous apply flow authorizes decision/offer/accept
            # by the applications.continuation_token issued at submit. A volume predating
            # 0008 has no column, so submit's UPDATE would 500 and no token could be issued
            # -- silently breaking anonymous apply. Fail readiness loud, naming the column,
            # same as the monthly_debt / uq_loans_app rungs.
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'applications' "
                "AND column_name = 'continuation_token'"
            )
            if cur.fetchone() is None:
                return False, "schema_not_ready:applications.continuation_token"
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
    # Fail loud on a missing internal-service token (PR review): without it every
    # internal-only route (monthly-debt, /board) fails closed AND origination's calls to
    # kyc/decision/disclosure carry no secret — the kyc call is caught in submit's
    # try/except and silently degrades CIP to all-false, so a misconfig would otherwise
    # look healthy while quietly breaking verification. Surface it at /health instead.
    if not INTERNAL_SERVICE_TOKEN:
        missing.append("INTERNAL_SERVICE_TOKEN")
    return missing


SERVICING_URL = os.getenv("SERVICING_URL", "http://servicing-service:8002")

# Extracted microservices the LOS now orchestrates over HTTP (formerly in-process:
# CIP/KYC, decisioning, and offer/disclosure). Defaults match the docker network.
KYC_URL = os.getenv("KYC_URL", "http://kyc-service:8003")
DECISION_URL = os.getenv("DECISION_URL", "http://decision-service:8004")
DISCLOSURE_URL = os.getenv("DISCLOSURE_URL", "http://disclosure-service:8005")

# Shared secret identifying an internal service-to-service call. Forwarded on the
# calls this service makes to decision-service (record read) and required by this
# service's own internal-only remediation route. Env only, no committed default; an
# unset token makes the internal routes fail closed. See docs/security-remediation.
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
