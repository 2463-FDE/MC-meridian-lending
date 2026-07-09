"""Decision service configuration.

Carried over from origination when decisioning was split into its own service.
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
    """True only when DATABASE_URL is set with a non-empty password.

    This stack authenticates to Postgres with a password: docker-compose sets
    POSTGRES_PASSWORD via ${VAR:?} and .env.example embeds it in the DSN. So an
    unset DATABASE_URL — or a present-but-passwordless one (meridian:@postgres,
    the shape the secret purge left behind) — is a misconfiguration, not a valid
    setup. Left unflagged it connects with no password (or fails opaquely at
    query time, swallowed by decide()), so the service looks OK while issuing
    decisions it cannot persist.

    NOTE: a deployment that intentionally uses passwordless auth (an IAM token,
    peer/trust, or the password supplied out-of-band via PGPASSWORD/.pgpass)
    must revisit this gate — here, passwordless means misconfigured.
    """
    if not DATABASE_URL:
        return False
    try:
        return bool(urlparse(DATABASE_URL).password)
    except ValueError:
        return False


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
