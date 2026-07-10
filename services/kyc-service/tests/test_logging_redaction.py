"""Allowlist-logging regression for kyc-service.

Neither the CIP check (kyc.run_cip) nor the /kyc/check route may log the raw
applicant identity (name/dob/ssn/address). The raw name is client-controlled
free text that could hide a PAN (and is PII in its own right); name/dob/address
have no self-identifying shape the redactor can key on. So the old full-payload
'req=%s' dump and the applicant=<name> line leaked identity into the service
log. Log only the operational ids + boolean result; the shared redactor stays a
backstop, but these log lines must not depend on it.
"""
import logging
import os
import tempfile
from pathlib import Path

import pytest

from app import kyc
from app.logging_config import get_logger
from app.routers import kyc as kyc_router
from app.schemas import CipCheckIn


@pytest.fixture
def temp_log_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("LOG_DIR", tmpdir)
        for name, mod in (("kyc", kyc), ("kyc-api", kyc_router)):
            logging.getLogger(name).handlers.clear()
            monkeypatch.setattr(mod, "log", get_logger(name))
        yield tmpdir


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


def test_run_cip_log_omits_name(temp_log_dir):
    kyc.run_cip({
        "applicant_id": 99, "name": "Jane Doe", "dob": "1970-01-01",
        "ssn": "412-55-9981", "address": "10 Main St",
    })
    content = (Path(temp_log_dir) / "kyc-service.log").read_text()
    assert "Jane Doe" not in content, "raw name reached the log"
    assert "412-55-9981" not in content, "raw SSN reached the log"
    assert "applicant_id=99" in content, "operational id should be logged"


def test_kyc_check_route_omits_direct_identifiers(temp_log_dir, monkeypatch):
    monkeypatch.setattr(kyc_router.db, "query", lambda *a, **k: [{"id": 1}])
    kyc_router.kyc_check(CipCheckIn(
        application_id=7, applicant_id=99, name="Jane Doe", dob="1970-01-01",
        ssn="412-55-9981", address="10 Main St", entity_type=None,
    ))
    content = (Path(temp_log_dir) / "kyc-service.log").read_text()
    assert "Jane Doe" not in content, "raw name reached the log"
    assert "1970-01-01" not in content, "DOB reached the log"
    assert "10 Main St" not in content, "address reached the log"
    assert "412-55-9981" not in content, "raw SSN reached the log"
    assert "req=" not in content, "full-payload dump reintroduced"
    assert "application_id=7" in content, "operational id should be logged"
