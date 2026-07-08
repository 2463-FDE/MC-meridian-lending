"""Logging with PII redaction.

Redacts PAN, CVV, SSN, email, phone before writing to logs.
Addresses PCI-DSS 3.4 (plaintext PII in logs).
"""
import logging
import os

from .redactor import PiiRedactor, _RedactWrapper, configure_uvicorn



class RedactingFormatter(logging.Formatter):
    """Custom formatter that redacts PII before writing logs."""

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        return PiiRedactor.redact(msg)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    # Own our handlers. Otherwise records propagate to root (uvicorn/basicConfig),
    # formatted from raw msg/args — unredacted duplicate on stdout/central collector.
    # Set unconditionally: a logger that ALREADY had handlers (attached by a test,
    # uvicorn, or a pre-existing config) must still be forced non-propagating and
    # have those handlers redacted below — the old early-return trusted them raw.
    logger.propagate = False
    fmt = RedactingFormatter("%(levelname)s %(asctime)s %(message)s")

    # Force redaction onto any handler already attached to this logger (by a test,
    # by uvicorn, or by a pre-existing logging config). Never trust an inherited
    # handler to redact — an unwrapped formatter writes raw PAN/CVV/SSN.
    for h in logger.handlers:
        if not isinstance(h.formatter, (RedactingFormatter, _RedactWrapper)):
            h.setFormatter(_RedactWrapper(h.formatter or fmt))

    # Install our stream + file handlers unless already present, so repeat calls
    # don't stack duplicates while a cleared logger is still re-armed.
    if not any(isinstance(h.formatter, RedactingFormatter) for h in logger.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)

        try:
            log_dir = os.getenv("LOG_DIR", "logs")
            os.makedirs(log_dir, exist_ok=True)
            fh = logging.FileHandler(os.path.join(log_dir, "servicing-service.log"))
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError:
            pass

    # Also redact uvicorn's own access/error loggers (URLs, tracebacks).
    configure_uvicorn(fmt)
    return logger
