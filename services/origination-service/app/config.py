"""Origination service configuration.

Bureau/DB credentials are now read from the environment only — no secret defaults
in source (was: inline "so the demo just works"). Inject via the host env /
secret manager; see docs/security-remediation-2026-07.md.
"""
import os

# --- Credit bureau (Experian) — env only; no committed default. Rotate the key
# that was previously hardcoded/committed. ---
EXPERIAN_KEY = os.getenv("EXPERIAN_KEY", "")
EXPERIAN_BASE_URL = os.getenv("EXPERIAN_BASE_URL", "https://api.experian.example.com/v2")

# Core banking key — env only; no committed default.
CORE_BANKING_API_KEY = os.getenv("CORE_BANKING_API_KEY", "")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:@postgres:5432/meridian",
)

SERVICING_URL = os.getenv("SERVICING_URL", "http://servicing-service:8002")

# Extracted microservices the LOS now orchestrates over HTTP (formerly in-process:
# CIP/KYC, decisioning, and offer/disclosure). Defaults match the docker network.
KYC_URL = os.getenv("KYC_URL", "http://kyc-service:8003")
DECISION_URL = os.getenv("DECISION_URL", "http://decision-service:8004")
DISCLOSURE_URL = os.getenv("DISCLOSURE_URL", "http://disclosure-service:8005")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
