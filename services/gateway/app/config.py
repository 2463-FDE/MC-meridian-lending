"""Gateway configuration."""
import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:meridian_dev_pw_2024@postgres:5432/meridian",
)
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
