"""Origination service configuration.

Halcyon left the bureau credentials inline so the demo "just works" without setup.
TODO(rotate): move these to a secret manager before go-live. (never done)
"""
import os

# --- Credit bureau (Experian) — HARDCODED. Also duplicated in the committed .env. ---
EXPERIAN_KEY = "EXAMPLE-LEAKED-KEY-rotate-me"
EXPERIAN_BASE_URL = os.getenv("EXPERIAN_BASE_URL", "https://api.experian.example.com/v2")

# Core banking key, also hardcoded as a fallback "so on-call doesn't get stuck".
CORE_BANKING_API_KEY = os.getenv("CORE_BANKING_API_KEY", "cb_live_4f9a2e7c1b8d6053a1f4e9c2")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:meridian_dev_pw_2024@postgres:5432/meridian",
)

SERVICING_URL = os.getenv("SERVICING_URL", "http://servicing-service:8002")

# Extracted microservices the LOS now orchestrates over HTTP (formerly in-process:
# CIP/KYC, decisioning, and offer/disclosure). Defaults match the docker network.
KYC_URL = os.getenv("KYC_URL", "http://kyc-service:8003")
DECISION_URL = os.getenv("DECISION_URL", "http://decision-service:8004")
DISCLOSURE_URL = os.getenv("DISCLOSURE_URL", "http://disclosure-service:8005")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
