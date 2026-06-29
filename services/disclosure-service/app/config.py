"""Disclosure service configuration."""
import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:meridian_dev_pw_2024@postgres:5432/meridian",
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
