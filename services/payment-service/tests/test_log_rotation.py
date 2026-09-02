"""Log files must rotate daily and expire (debt D5 residual).

The redactor keeps PII out of a log LINE; nothing bounded the log FILE. Every
service installed a plain `logging.FileHandler`, so `logs/<service>.log` grew
without limit and no line was ever deleted -- including the lines written before
PR #2 added redaction, which are the plaintext PAN/CVV/SSN D5 was opened for.
GLBA Safeguards Rule 314.4(c)(6) and PCI-DSS 3.1 both require a disposal
procedure, and a file nothing ever truncates cannot satisfy one.
"""

import logging
import logging.handlers
import os
import tempfile
import uuid
from datetime import date, timedelta
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


def test_the_log_file_handler_rotates_daily_and_keeps_a_bounded_history(temp_log_dir):
    # A fresh logger name every run: get_logger only installs handlers on a logger
    # that has none of ours, so a reused name would assert against a cached handler.
    logger = get_logger(f"rotation_{uuid.uuid4().hex}")
    handler = _file_handler(logger)

    assert isinstance(handler, logging.handlers.TimedRotatingFileHandler)
    assert handler.when == "MIDNIGHT"
    assert logging_config.LOG_RETENTION_DAYS == RETENTION_DAYS
    assert handler.backupCount == RETENTION_DAYS
    # UTC, so a container's local timezone cannot move the rollover boundary and
    # two services on one host cannot disagree about which day a line belongs to.
    assert handler.utc is True


def test_rotated_files_past_the_retention_window_are_nominated_for_deletion(
    temp_log_dir,
):
    logger = get_logger(f"retention_{uuid.uuid4().hex}")
    handler = _file_handler(logger)

    base = Path(handler.baseFilename)
    start = date(2026, 1, 1)
    # Two days more than the window keeps, so the two oldest must be dropped.
    for offset in range(RETENTION_DAYS + 2):
        rotated = base.parent / f"{base.name}.{(start + timedelta(days=offset))}"
        rotated.write_text("")

    doomed = sorted(Path(p).name for p in handler.getFilesToDelete())

    assert doomed == [f"{base.name}.2026-01-01", f"{base.name}.2026-01-02"]
