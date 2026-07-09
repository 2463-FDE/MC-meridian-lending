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

# Local/demo escape hatch. When true, a missing EXPERIAN_KEY or a bureau failure
# falls back to a deterministic SYNTHETIC credit score so the stack runs without a
# live bureau. MUST be false in any real environment: otherwise the service issues
# approvals/denials without a real credit pull. Default false = fail closed.
ALLOW_SYNTHETIC_CREDIT = os.getenv("ALLOW_SYNTHETIC_CREDIT", "").strip().lower() in (
    "1", "true", "yes", "on",
)


def missing_required_secrets() -> list:
    """Secrets that MUST be present in a production-like (non-synthetic) config.

    Used by /health so a deployment with no bureau key reports unhealthy instead
    of looking OK while silently issuing decisions off a stub score.
    """
    missing = []
    if not ALLOW_SYNTHETIC_CREDIT and not EXPERIAN_KEY:
        missing.append("EXPERIAN_KEY")
    return missing

# Core banking key — env only; no committed default.
CORE_BANKING_API_KEY = os.getenv("CORE_BANKING_API_KEY", "")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:@postgres:5432/meridian",
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
