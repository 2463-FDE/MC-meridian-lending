"""Allowlist-logging regression for kyc-service.

Neither the CIP check (kyc.run_cip) nor the /kyc/check route may log the raw
applicant identity (name/dob/ssn/address). The redactor masks ssn but has no
shape to key on for name/dob/address, so the old full-payload 'req=%s' dump and
the applicant=<name> line leaked identity into the service log. Log only the
operational ids + boolean result.
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
