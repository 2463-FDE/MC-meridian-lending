"""Log files must rotate daily and expire (debt D5 residual).

The redactor keeps PII out of a log LINE; nothing bounded the log FILE. Every
service installed a plain `logging.FileHandler`, so `logs/<service>.log` grew
without limit and no line was ever deleted -- including the lines written before
PR #2 added redaction, which are the plaintext PAN/CVV/SSN D5 was opened for.
GLBA Safeguards Rule 314.4(c)(6) and PCI-DSS 3.1 both require a disposal
procedure, and a file nothing ever truncates cannot satisfy one.

Two properties beyond "a rotating handler is installed" are asserted here, both
because the first implementation of this control had them wrong.

ONE WRITER PER FILE. get_logger installs handlers per LOGGER and a service
creates several named loggers, so a handler each put several rotating writers on
one file. The first to cross midnight renames the file and reopens it; the others
keep the old file descriptor -- now the dated backup -- and stdlib doRollover()
returns early once that backup exists, before closing the stream and before
advancing rolloverAt. Those writers append to the rotated file forever and retry
the rollover on every record, so retention holds nothing.

DISPOSAL BY AGE, NOT BY FILE COUNT. `backupCount` bounds the number of files,
which is a number of days only while the service rotates daily. A retention claim
stated in days has to be asserted in days.
"""

import logging
import logging.handlers
import os
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import logging_config
from app.logging_config import get_logger

# D5's mitigation path names a 30-day retention. Pinned here rather than read off
# the module so this file states the requirement and the assertion below is what
# proves the implementation agrees with it.
RETENTION_DAYS = 30


@pytest.fixture
def temp_log_dir():
    """Point LOG_DIR at a throwaway directory for the duration of one test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original = os.getenv("LOG_DIR")
        os.environ["LOG_DIR"] = tmpdir
        yield tmpdir
        if original is not None:
            os.environ["LOG_DIR"] = original
        else:
            os.environ.pop("LOG_DIR", None)


def _file_handler(logger: logging.Logger) -> logging.FileHandler:
    handlers = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
    assert len(handlers) == 1, f"expected exactly one file handler, got {handlers}"
    return handlers[0]


def _fresh_logger(prefix: str) -> logging.Logger:
    # A fresh logger name every run: get_logger only installs handlers on a logger
    # that has none of ours, so a reused name would assert against a cached handler.
    return get_logger(f"{prefix}_{uuid.uuid4().hex}")


def _dated(base_name: str, suffix: str, today, age_days: int) -> str:
    """The name stdlib would give a backup holding lines `age_days` days old."""
    return f"{base_name}.{(today - timedelta(days=age_days)).strftime(suffix)}"


def test_the_log_file_handler_rotates_daily_and_keeps_a_bounded_history(temp_log_dir):
    logger = _fresh_logger("rotation")
    handler = _file_handler(logger)

    assert isinstance(handler, logging.handlers.TimedRotatingFileHandler)
    assert isinstance(handler, logging_config.ExpiringTimedRotatingFileHandler)
    assert handler.when == "MIDNIGHT"
    assert logging_config.LOG_RETENTION_DAYS == RETENTION_DAYS
    assert handler.backupCount == RETENTION_DAYS
    # UTC, so a container's local timezone cannot move the rollover boundary and
    # two services on one host cannot disagree about which day a line belongs to.
    assert handler.utc is True


def test_every_logger_in_the_process_shares_one_writer_on_the_log_file(temp_log_dir):
    # Several named loggers is the normal case, not an edge one: origination builds
    # eight (intake, assistant, clients, authz, llm, policy_retrieval, ...).
    first = _fresh_logger("shared_a")
    second = _fresh_logger("shared_b")

    assert _file_handler(first) is _file_handler(second), (
        "two rotating writers on one log file: whichever one does not roll keeps "
        "writing into the dated backup and never rotates again"
    )


def test_after_a_rollover_every_logger_writes_to_the_new_active_file(temp_log_dir):
    first = _fresh_logger("rollover_a")
    second = _fresh_logger("rollover_b")
    handler = _file_handler(first)
    base = Path(handler.baseFilename)

    first.info("before rollover from the first logger")
    second.info("before rollover from the second logger")

    # Force the next record to cross the boundary, rather than waiting for midnight.
    handler.rolloverAt = int(time.time()) - 1

    first.info("after rollover from the first logger")
    second.info("after rollover from the second logger")

    backups = sorted(
        p for p in base.parent.iterdir() if p.name.startswith(base.name + ".")
    )
    assert len(backups) == 1, f"expected exactly one rotated file, got {backups}"

    active = base.read_text()
    assert "after rollover from the first logger" in active
    # The defect this pins: the second logger's own writer still held the
    # pre-rename descriptor, so its post-rollover lines landed in the backup.
    assert "after rollover from the second logger" in active, (
        "a logger kept writing into the rotated backup after the rollover"
    )

    rotated = backups[0].read_text()
    assert "before rollover from the first logger" in rotated
    assert "before rollover from the second logger" in rotated


def test_a_rotated_file_is_deleted_once_its_oldest_line_reaches_the_window(
    temp_log_dir,
):
    handler = _file_handler(_fresh_logger("retention"))
    base = Path(handler.baseFilename)
    today = datetime.now(timezone.utc).date()

    # Weekly, not daily: sparse rotation is exactly what backupCount cannot express
    # (30 weekly files span 30 WEEKS), plus the two files that straddle the boundary.
    ages = sorted(
        {7 * weeks for weeks in range(12)} | {RETENTION_DAYS - 1, RETENTION_DAYS}
    )
    for age in ages:
        (base.parent / _dated(base.name, handler.suffix, today, age)).write_text("")

    doomed = {Path(p).name for p in handler.getFilesToDelete()}

    for age in ages:
        name = _dated(base.name, handler.suffix, today, age)
        if age >= RETENTION_DAYS:
            assert name in doomed, f"a file holding {age}-day-old lines was retained"
        else:
            assert name not in doomed, f"a file holding {age}-day-old lines was deleted"


def test_an_expired_file_is_deleted_when_the_handler_is_created(temp_log_dir):
    # stdlib deletes only inside doRollover(), so an idle process disposes of
    # nothing. Learn the service's log file name from a handler in one directory,
    # then plant backups in a second one and build the handler there -- one path
    # gets one handler, so the purge under test has to run on a path not yet used.
    probe = _file_handler(_fresh_logger("probe"))
    base_name = Path(probe.baseFilename).name
    today = datetime.now(timezone.utc).date()

    fresh = Path(temp_log_dir) / "fresh"
    fresh.mkdir()
    expired = fresh / _dated(base_name, probe.suffix, today, RETENTION_DAYS)
    inside = fresh / _dated(base_name, probe.suffix, today, 1)
    expired.write_text("plaintext from before redaction\n")
    inside.write_text("inside the window\n")

    os.environ["LOG_DIR"] = str(fresh)
    handler = _file_handler(_fresh_logger("purge"))
    assert Path(handler.baseFilename).parent == fresh

    assert not expired.exists(), "an expired backup survived handler construction"
    assert inside.exists(), "a backup inside the retention window was deleted"
