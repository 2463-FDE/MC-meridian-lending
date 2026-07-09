"""KYC service configuration.

Carried over from origination when the CIP logic was split into its own service.
Bureau/DB credentials are now read from the environment only — no secret defaults
in source (was: inline "so the demo just works"). Inject via the host env /
secret manager; see docs/security-remediation-2026-07.md.
"""
import os
from urllib.parse import urlparse

# --- Credit bureau (Experian) — env only; no committed default. Rotate the key
# that was previously hardcoded/committed. ---
EXPERIAN_KEY = os.getenv("EXPERIAN_KEY", "")
EXPERIAN_BASE_URL = os.getenv("EXPERIAN_BASE_URL", "https://api.experian.example.com/v2")

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
    # Known placeholder / previously-committed stub passwords are never valid.
    if password.lower() in {
        "replace_with_postgres_password",
        "meridian_dev_pw_2024",
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
    """Config that MUST be present for a healthy runtime; surfaced by /health so
    an unset or passwordless DATABASE_URL reports unhealthy instead of connecting
    unauthenticated (or failing opaquely at query time) while looking OK."""
    missing = []
    if not database_url_configured():
        missing.append("DATABASE_URL")
    return missing

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
