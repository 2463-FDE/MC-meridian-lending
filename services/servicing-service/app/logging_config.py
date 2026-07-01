"""Logging with PII redaction.

Redacts PAN, CVV, SSN, email, phone before writing to logs.
Addresses PCI-DSS 3.4 (plaintext PII in logs).
"""
import logging
import os

from .redactor import PiiRedactor

LOG_DIR = os.getenv("LOG_DIR", "logs")


class RedactingFormatter(logging.Formatter):
    """Custom formatter that redacts PII before writing logs."""

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        return PiiRedactor.redact(msg)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    fmt = RedactingFormatter("%(levelname)s %(asctime)s %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = logging.FileHandler(os.path.join(LOG_DIR, "servicing-service.log"))
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    return logger
