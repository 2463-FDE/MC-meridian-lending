"""Gateway configuration."""
import os
from urllib.parse import urlparse

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
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

ORIGINATION_URL = os.getenv("ORIGINATION_URL", "http://origination-service:8001")
SERVICING_URL = os.getenv("SERVICING_URL", "http://servicing-service:8002")
KYC_URL = os.getenv("KYC_URL", "http://kyc-service:8003")
DECISION_URL = os.getenv("DECISION_URL", "http://decision-service:8004")
DISCLOSURE_URL = os.getenv("DISCLOSURE_URL", "http://disclosure-service:8005")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment-service:8006")

# 8-hour sessions. (No refresh, no rotation, no CSRF token — Halcyon "v1 auth".)
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "28800"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
