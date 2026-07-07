"""Logging with PII redaction.

Redacts PAN, CVV, SSN, email, phone before writing to logs.
Addresses PCI-DSS 3.4 (plaintext PII in logs).
"""
import logging
import os

from .redactor import PiiRedactor, configure_uvicorn



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
    # Own our handlers. Otherwise records propagate to root (uvicorn/basicConfig),
    # formatted from raw msg/args — unredacted duplicate on stdout/central collector.
    logger.propagate = False
    fmt = RedactingFormatter("%(levelname)s %(asctime)s %(name)s %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    try:
        log_dir = os.getenv("LOG_DIR", "logs")
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.FileHandler(os.path.join(log_dir, "decision-service.log"))
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    # Also redact uvicorn's own access/error loggers (URLs, tracebacks).
    configure_uvicorn(fmt)
    return logger
