"""KYC persistence + boundary contract (ADR 0011, PR review).

The persisted kyc_checks row -- not the /kyc/check response -- is the gate for
decision/offer/boarding (origination require_kyc_passed). Two invariants:

1. A successful KYC response is impossible without a persisted row. If the insert
   fails, the route fails closed (503) rather than reporting status=pass with no row,
   which would strand the application with no in-product recovery path.
2. dob/ssn/address are optional at the boundary (mirrors origination ApplicationIn and
   the entity-applicant path), so a partial/entity request produces a persisted pass/fail
   row instead of a 422 that origination misclassifies as KYC unavailability.
"""

from fastapi.testclient import TestClient

from app.main import app
from app.routers import kyc as kyc_router

TOKEN = "test-internal-token"


def _auth(monkeypatch):
    monkeypatch.setattr(kyc_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)


def _headers():
    return {"X-Internal-Service": TOKEN}


def test_insert_failure_fails_closed_not_pass(monkeypatch):
    _auth(monkeypatch)

    def _explode(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(kyc_router.db, "query", _explode)
    resp = TestClient(app, raise_server_exceptions=False).post(
        "/kyc/check",
        json={
            "application_id": 7,
            "applicant_id": 99,
            "name": "Jane Doe",
            "dob": "1970-01-01",
            "ssn": "412-55-9981",
            "address": "10 Main St",
        },
        headers=_headers(),
    )
    assert resp.status_code == 503


def test_insert_returning_no_id_fails_closed(monkeypatch):
    _auth(monkeypatch)
    monkeypatch.setattr(kyc_router.db, "query", lambda *a, **k: [])
    resp = TestClient(app, raise_server_exceptions=False).post(
        "/kyc/check",
        json={
            "application_id": 7,
            "applicant_id": 99,
            "name": "Jane Doe",
            "dob": "1970-01-01",
            "ssn": "412-55-9981",
            "address": "10 Main St",
        },
        headers=_headers(),
    )
    assert resp.status_code == 503


def test_entity_applicant_no_dob_ssn_persists_and_passes(monkeypatch):
    _auth(monkeypatch)
    persisted = {}

    def _capture(_sql, params):
        persisted["params"] = params
        return [{"id": 5}]

    monkeypatch.setattr(kyc_router.db, "query", _capture)
    resp = TestClient(app).post(
        "/kyc/check",
        json={
            "application_id": 7,
            "applicant_id": 99,
            "name": "Acme LLC",
            "address": "10 Main St",
            "entity_type": "llc",
        },
        headers=_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    # name + address present => CIP passes; no dob/ssn => those columns false.
    assert body["cip_passed"] is True
    assert body["status"] == "pass"
    assert body["check_id"] == 5
    # persisted row reflects the missing dob/ssn as unverified, not a 422.
    _applicant_id, name_v, dob_v, addr_v, ssn_v = persisted["params"]
    assert (name_v, addr_v) == (True, True)
    assert (dob_v, ssn_v) == (False, False)


def test_missing_address_persists_a_fail_row(monkeypatch):
    _auth(monkeypatch)
    monkeypatch.setattr(kyc_router.db, "query", lambda *a, **k: [{"id": 6}])
    resp = TestClient(app).post(
        "/kyc/check",
        json={
            "application_id": 7,
            "applicant_id": 99,
            "name": "Jane Doe",
        },
        headers=_headers(),
    )
    # No address => CIP fails honestly, but a row is still persisted (200, status=fail).
    assert resp.status_code == 200
    body = resp.json()
    assert body["cip_passed"] is False
    assert body["status"] == "fail"
    assert body["check_id"] == 6
