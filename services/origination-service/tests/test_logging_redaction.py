"""Allowlist-logging regression for origination intake.

create_application() must log only non-identifying operational fields — never
the direct applicant identifiers (name/dob/ssn/ein/address). The log redactor
masks ssn but has no shape to key on for name/dob/ein/address, so a full-payload
dump (the old D5 'req=%s' line) leaked applicant identity into the service log.
Exercises the real intake.create_application path with the DB stubbed.
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
        monkeypatch.setenv("LOG_DIR", tmpdir)
        logging.getLogger("intake").handlers.clear()
        monkeypatch.setattr(intake, "log", get_logger("intake"))
        # DB is not under test — stub both INSERTs to return a row with an id.
        monkeypatch.setattr(intake.db, "query", lambda *a, **k: [{"id": 1}])
        yield tmpdir


def test_intake_log_omits_direct_identifiers(temp_log_dir):
    intake.create_application({
        "name": "Jane Doe",
        "dob": "1970-01-01",
        "address": "10 Main St",
        "ein": "12-3456789",
        "ssn": "412-55-9981",
        "amount": 18000,
        "term_months": 48,
        "purpose": "auto",
        "is_entity": False,
    })
    content = (Path(temp_log_dir) / "origination-service.log").read_text()
    assert "Jane Doe" not in content, "raw name reached the log"
    assert "1970-01-01" not in content, "DOB reached the log"
    assert "10 Main St" not in content, "address reached the log"
    assert "12-3456789" not in content, "EIN reached the log"
    assert "412-55-9981" not in content, "raw SSN reached the log"
    assert "req=" not in content, "full-payload dump reintroduced"
    # Operational fields the officer needs for triage are still logged.
    assert "amount=18000" in content
    assert "purpose=auto" in content
