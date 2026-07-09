"""Allowlist-logging regression for kyc-service.

run_cip() must log only the applicant_id + boolean CIP result — never the raw
name (client-controlled free text that could hide a PAN, and PII in its own
right). This closes the "PAN hidden in a free-text field reaches the log" class
at the source. The shared redactor stays a backstop (covered by the origination
redactor unit tests), but the CIP log must not depend on it. Exercises the real
kyc.run_cip path (pure — no DB).
"""
import logging
import os
import tempfile
from pathlib import Path

import pytest

from app import kyc
from app.logging_config import get_logger


@pytest.fixture
def temp_log_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        original = os.getenv("LOG_DIR")
        os.environ["LOG_DIR"] = tmpdir
        logging.getLogger("kyc").handlers.clear()
        monkeypatch.setattr(kyc, "log", get_logger("kyc"))
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
def test_cip_log_omits_name_and_hidden_pan(temp_log_dir, hidden_pan):
    kyc.run_cip({
        "applicant_id": 99,
        "name": hidden_pan,
        "dob": "1990-01-01",
        "ssn": "412-55-9981",
        "address": "12 Main St",
    })
    content = (Path(temp_log_dir) / "kyc-service.log").read_text()
    assert "4111" not in content, f"PAN reached the log for {hidden_pan!r}"
    assert "411111111111" not in content
    assert "412-55-9981" not in content, "SSN reached the log"
    assert "applicant_id=99" in content, "operational id should be logged"


def test_cip_log_is_not_raw_payload_dump(temp_log_dir):
    kyc.run_cip({"applicant_id": 7, "name": "Jane Doe", "ssn": "412-55-9981",
                 "address": "12 Main St"})
    content = (Path(temp_log_dir) / "kyc-service.log").read_text()
    assert "Jane" not in content, "raw name reached the log"
    assert "req=" not in content, "raw payload dump reintroduced"
