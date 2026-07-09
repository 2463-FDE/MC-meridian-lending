import os
from urllib.parse import unquote, urlparse

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
    """Config that MUST be present for a healthy runtime; surfaced by /health so
    an unset or passwordless DATABASE_URL reports unhealthy instead of connecting
    unauthenticated (or failing opaquely at query time) while looking OK."""
    missing = []
    if not database_url_configured():
        missing.append("DATABASE_URL")
    return missing


# Processor key — env only; no committed default. Rotate the previously-committed
# key (see docs/security-remediation-2026-07.md).
PROCESSOR_API_KEY = os.getenv("PROCESSOR_API_KEY", "")
PROCESSOR_BASE_URL = os.getenv("PROCESSOR_BASE_URL", "https://api.cardprocessor.example.com")
SETTLEMENT_FILE = os.getenv("SETTLEMENT_FILE", "data/settlement.csv")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
