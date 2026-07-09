"""Allowlist-logging regression for origination intake.

create_application() must log only an allowlist of non-PII, non-free-text
fields (amount / term / entity flag) — never the raw request dict. This closes
the whole class of "PAN hidden in a free-text field reaches the log" bypasses at
the source: client free text simply never gets logged. The shared redactor stays
a backstop (covered by test_redactor.py), but the intake log must not depend on
it. Exercises the real intake.create_application path with the DB mocked.
"""
import logging
import os
import tempfile
from pathlib import Path

import pytest

from app import intake
from app.logging_config import get_logger


@pytest.fixture
def temp_log_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        original = os.getenv("LOG_DIR")
        os.environ["LOG_DIR"] = tmpdir
        logging.getLogger("intake").handlers.clear()
        # Rebuild intake's module logger so it writes into this temp dir.
        monkeypatch.setattr(intake, "log", get_logger("intake"))
        # Mock the DB so create_application runs without a real connection.
        monkeypatch.setattr(intake.db, "query", lambda *a, **k: [{"id": 1}])
        yield tmpdir
        if original:
            os.environ["LOG_DIR"] = original
        else:
            os.environ.pop("LOG_DIR", None)


@pytest.mark.parametrize("hidden_pan", [
    "4111x1111x1111x1111",            # letter separators
    "4111====1111====1111====1111",   # long separator runs
    "4111111111111111",               # bare card
])
def test_intake_log_omits_pii_and_hidden_pan(temp_log_dir, hidden_pan):
    """A PAN hidden in the free-text name/address never reaches the log, because
    those fields are not logged at all. SSN/name likewise absent."""
    intake.create_application({
        "name": hidden_pan,
        "address": f"{hidden_pan} Main St",
        "ssn": "412-55-9981",
        "amount": 18000,
        "term_months": 48,
        "is_entity": False,
    })
    content = (Path(temp_log_dir) / "origination-service.log").read_text()
    # No card digits, no SSN, no name field label.
    assert "4111" not in content, f"PAN reached the log for {hidden_pan!r}"
    assert "411111111111" not in content
    assert "412-55-9981" not in content and "9981" not in content, "SSN reached the log"
    assert "name" not in content and "address" not in content, "PII field was logged"
    # Allowlisted operational fields ARE present.
    assert "18000" in content and "48" in content


def test_intake_log_is_not_raw_payload_dump(temp_log_dir):
    """Guard against a regression to `req=%s` dumping the whole dict."""
    intake.create_application({"name": "Jane Doe", "ssn": "412-55-9981",
                               "amount": 9000, "term_months": 36})
    content = (Path(temp_log_dir) / "origination-service.log").read_text()
    assert "req=" not in content, "raw payload dump reintroduced"
    assert "Jane" not in content
