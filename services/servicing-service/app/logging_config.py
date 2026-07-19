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
        # A prior handler's plain formatter may have already cached record.exc_text
        # RAW (stdlib caches the traceback on first format()). stdlib format() then
        # SKIPS formatException when exc_text is already set, so our formatException
        # override never runs and the raw traceback would be appended verbatim.
        # Redact the cached copy in place before super() appends it.
        if record.exc_text:
            record.exc_text = PiiRedactor.redact(record.exc_text)
        return super().format(record)

    def formatMessage(self, record: logging.LogRecord) -> str:
        # Redact the MESSAGE only (args already expanded) -- never the levelname/asctime
        # prefix. Redacting the whole formatted line let a Luhn-valid timestamp digit run
        # (YYYYMMDDHHMMSSmmm) be masked as a false PAN: corrupted timestamps + time-flaky
        # redaction tests. record.message is transient (re-derived every format() call), so
        # mutating it here does not affect other handlers.
        record.message = PiiRedactor.redact(record.message)
        return super().formatMessage(record)

    def formatException(self, ei) -> str:
        # Tracebacks can carry PII -- still redacted (appended after the timestamp prefix).
        return PiiRedactor.redact(super().formatException(ei))

    def formatStack(self, stack_info: str) -> str:
        return PiiRedactor.redact(super().formatStack(stack_info))


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
