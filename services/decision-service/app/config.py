"""Decision service configuration.

Carried over from origination when decisioning was split into its own service.
Bureau/DB credentials are now read from the environment only — no secret defaults
in source (was: inline "so the demo just works"). Inject via the host env /
secret manager; see docs/security-remediation-2026-07.md.
"""
import os

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


def missing_required_secrets() -> list:
    """Secrets that MUST be present in a production-like (non-synthetic) config.

    Used by /health so a deployment with no bureau key reports unhealthy instead
    of looking OK while silently issuing decisions off a stub score.
    """
    missing = []
    if not synthetic_credit_enabled() and not EXPERIAN_KEY:
        missing.append("EXPERIAN_KEY")
    return missing

# Core banking key — env only; no committed default.
CORE_BANKING_API_KEY = os.getenv("CORE_BANKING_API_KEY", "")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:@postgres:5432/meridian",
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
