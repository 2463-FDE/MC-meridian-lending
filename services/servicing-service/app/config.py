import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:@postgres:5432/meridian",
)
# Processor key — env only; no committed default. Rotate the previously-committed
# key (see docs/security-remediation-2026-07.md).
PROCESSOR_API_KEY = os.getenv("PROCESSOR_API_KEY", "")
PROCESSOR_BASE_URL = os.getenv("PROCESSOR_BASE_URL", "https://api.cardprocessor.example.com")
SETTLEMENT_FILE = os.getenv("SETTLEMENT_FILE", "data/settlement.csv")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
