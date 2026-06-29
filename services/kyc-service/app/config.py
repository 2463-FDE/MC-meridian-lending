"""KYC service configuration.

Carried over from origination when the CIP logic was split into its own service.
Halcyon left the bureau credentials inline so the demo "just works" without setup.
TODO(rotate): move these to a secret manager before go-live. (never done)
"""
import os

# --- Credit bureau (Experian) — HARDCODED. Also duplicated in the committed .env. ---
EXPERIAN_KEY = "EXAMPLE-LEAKED-KEY-rotate-me"
EXPERIAN_BASE_URL = os.getenv("EXPERIAN_BASE_URL", "https://api.experian.example.com/v2")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:meridian_dev_pw_2024@postgres:5432/meridian",
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
