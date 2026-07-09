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
    """True only when DATABASE_URL is set with a non-empty password.

    Password auth is how this stack reaches Postgres (compose sets
    POSTGRES_PASSWORD via ${VAR:?}; .env.example embeds it in the DSN), so an
    unset DATABASE_URL — or a present-but-passwordless one (meridian:@postgres,
    what the secret purge left behind) — is a misconfiguration reported
    unhealthy via missing_required_secrets(). A deploy using passwordless auth
    (IAM token, peer/trust, PGPASSWORD/.pgpass) must revisit this gate.
    """
    if not DATABASE_URL:
        return False
    try:
        return bool(urlparse(DATABASE_URL).password)
    except ValueError:
        return False


def missing_required_secrets() -> list:
    """Config that MUST be present for a healthy runtime; surfaced by /health so
    an unset or passwordless DATABASE_URL reports unhealthy instead of connecting
    unauthenticated (or failing opaquely at query time) while looking OK."""
    missing = []
    if not database_url_configured():
        missing.append("DATABASE_URL")
    return missing

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
