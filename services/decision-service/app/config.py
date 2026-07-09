"""Decision service configuration.

Carried over from origination when decisioning was split into its own service.
Bureau/DB credentials are now read from the environment only — no secret defaults
in source (was: inline "so the demo just works"). Inject via the host env /
secret manager; see docs/security-remediation-2026-07.md.
"""
import os
from urllib.parse import unquote, urlparse

# --- Credit bureau (Experian) — env only; no committed default. Rotate the key
# that was previously hardcoded/committed. ---
EXPERIAN_KEY = os.getenv("EXPERIAN_KEY", "")
EXPERIAN_BASE_URL = os.getenv("EXPERIAN_BASE_URL", "https://api.experian.example.com/v2")

# Deployment environment. Synthetic credit is gated on this being exactly
# "development", so no production config can enable it — not even by mistake.
ENVIRONMENT = os.getenv("ENVIRONMENT", "production").strip().lower()

# Local/demo escape hatch. When enabled, a missing EXPERIAN_KEY or a bureau
# failure falls back to a deterministic SYNTHETIC credit score so the stack runs
# without a live bureau. Guarded by TWO independent conditions (see
# synthetic_credit_enabled): the explicit opt-in flag AND ENVIRONMENT=development.
ALLOW_SYNTHETIC_CREDIT = os.getenv("ALLOW_SYNTHETIC_CREDIT", "").strip().lower() in (
    "1", "true", "yes", "on",
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
    (e.g. an external managed DB whose secret lives only in the DSN) needs a live
    connectivity probe — a follow-up. Passwordless auth (IAM/peer/PGPASSWORD)
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
        "changeme", "change_me", "password", "postgres",
    }:
        return False
    # When POSTGRES_PASSWORD is the source of truth (compose ${VAR:?}), the DSN
    # password must match it — catches a stale/placeholder DSN without a DB call.
    expected = os.getenv("POSTGRES_PASSWORD")
    if expected and password != expected:
        return False
    return True


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
    return missing

# Core banking key — env only; no committed default.
CORE_BANKING_API_KEY = os.getenv("CORE_BANKING_API_KEY", "")

# No committed default: a passwordless fallback DSN (meridian:@postgres) would
# let a deploy that omits DATABASE_URL connect unauthenticated and look healthy.
# Unset/passwordless is reported unhealthy via missing_required_secrets().
DATABASE_URL = os.getenv("DATABASE_URL", "")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
