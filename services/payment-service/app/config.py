import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:@postgres:5432/meridian",
)
# Processor key — env only; no committed default. Rotate the previously-committed
# key (see docs/security-remediation-2026-07.md).
PROCESSOR_API_KEY = os.getenv("PROCESSOR_API_KEY", "")
PROCESSOR_BASE_URL = os.getenv("PROCESSOR_BASE_URL", "https://api.cardprocessor.example.com")
# servicing-service base URL — we call it to apply a captured payment to the balance
SERVICING_URL = os.getenv("SERVICING_URL", "http://servicing-service:8002")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
