"""Logging with PII redaction.

Redacts PAN, CVV, SSN, email, phone before writing to logs.
Addresses PCI-DSS 3.4 (plaintext PII in logs).
"""
import logging
import logging.handlers
import os
import threading
from datetime import datetime, timedelta, timezone

from .redactor import PiiRedactor, _RedactWrapper, configure_uvicorn

# Debt D5 residual: rotated at UTC midnight, and only this many days of rotated
# files are kept. The redactor masks a log LINE; retention is what disposes of the
# FILE, which nothing did -- including the pre-redaction files that still hold
# plaintext PAN/CVV/SSN. GLBA Safeguards Rule 314.4(c)(6) and PCI-DSS 3.1 both
# require a disposal procedure; an unbounded file cannot satisfy one.
LOG_RETENTION_DAYS = 30


class ExpiringTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """Daily rotation that disposes by AGE as well as by file count.

    `backupCount` bounds the NUMBER of rotated files, which equals a number of days
    only while the service rotates every day. Rotation happens when a record is
    emitted after the boundary, so a service that is down -- or one that logs
    nothing for a stretch -- rotates less often, and `backupCount` files then hold
    lines far older than the window. The disposal duty D5 is open for is stated in
    days (GLBA Safeguards Rule 314.4(c)(6), PCI-DSS 3.1), so nominate by the file's
    own date too: a rotated file goes at the first rollover on or after the day its
    oldest line turns LOG_RETENTION_DAYS old. Under daily rotation the date rule and
    `backupCount` nominate the same file; the date rule is what holds when rotation
    is sparse, and it is the one the retention claim in docs/debt-log.md states.

    Residual, stated because the claim depends on it: stdlib deletes only inside
    doRollover(), so an idle process disposes of nothing. __init__ purges once for
    that reason -- every service builds its loggers at import -- which bounds the
    gap to one process lifetime rather than to the next record written.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        for path in self._expired_files():
            try:
                os.remove(path)
            except OSError:
                # Another process's handler on the same file may have removed it
                # first. Disposal is the goal; who performed it does not matter.
                pass

    def _expired_files(self) -> list[str]:
        """Rotated files whose oldest line is at least LOG_RETENTION_DAYS old."""
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=LOG_RETENTION_DAYS)
        directory = os.path.dirname(self.baseFilename)
        prefix = os.path.basename(self.baseFilename) + "."
        expired: list[str] = []
        try:
            names = os.listdir(directory)
        except OSError:
            # Unreadable log directory disposes of nothing; it must not take the
            # rollover (or the process) down with it.
            return expired
        for name in names:
            if not name.startswith(prefix):
                continue
            try:
                # self.suffix, not a literal: it is what named the file.
                stamped = datetime.strptime(name[len(prefix) :], self.suffix).date()
            except ValueError:
                continue  # not one of our dated backups
            if stamped <= cutoff:
                expired.append(os.path.join(directory, name))
        return expired

    def getFilesToDelete(self) -> list[str]:
        return sorted(set(super().getFilesToDelete()) | set(self._expired_files()))


# One rotating writer per log path, shared by every logger in this process.
# get_logger installs handlers per LOGGER, and a service creates several named
# loggers (intake, assistant, clients, ...), so a handler each meant several
# rotating writers on one file. The first to cross midnight renames the file and
# reopens it; the others keep the old file descriptor -- now the dated backup --
# and stdlib doRollover() returns early once that backup exists, BEFORE closing the
# stream and BEFORE advancing rolloverAt, so they keep appending to the rotated
# file and retry the rollover on every record. Retention then holds nothing: the
# lines sit in a file named for a day they were not written on, and disposal of
# that file destroys current logs while the active file is missing them.
#
# The cache's scope is the process, and that is the whole fix only because every
# service runs a single uvicorn process (no --workers, no gunicorn, in any
# services/*/Dockerfile). Adding a worker flag puts one writer per worker back on
# one file and needs a per-worker log path or a QueueHandler, not this dict.
_FILE_HANDLERS: dict[str, ExpiringTimedRotatingFileHandler] = {}
_FILE_HANDLERS_LOCK = threading.Lock()


def _shared_file_handler(
    path: str, fmt: logging.Formatter
) -> ExpiringTimedRotatingFileHandler:
    """The one handler for `path`, creating it on first use. Thread-safe: two
    loggers built concurrently must not each create a writer on the same file."""
    with _FILE_HANDLERS_LOCK:
        handler = _FILE_HANDLERS.get(path)
        if handler is None:
            handler = ExpiringTimedRotatingFileHandler(
                path, when="midnight", backupCount=LOG_RETENTION_DAYS, utc=True
            )
            handler.setFormatter(fmt)
            _FILE_HANDLERS[path] = handler
        return handler


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
    fmt = RedactingFormatter("%(levelname)s %(asctime)s %(name)s %(message)s")

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
            logger.addHandler(
                _shared_file_handler(
                    os.path.join(log_dir, "disclosure-service.log"), fmt
                )
            )
        except OSError:
            pass

    # Also redact uvicorn's own access/error loggers (URLs, tracebacks).
    configure_uvicorn(fmt)
    return logger
