"""POST /kyc/check is internal-only (PR review).

The CIP check persists an identity-verification record (kyc_checks) from caller-
supplied inputs and is reachable through the gateway's anonymous /kyc proxy. An
external caller must be refused before any CIP run or DB write.
"""

from fastapi.testclient import TestClient

from app.main import app
from app.routers import kyc as kyc_router

TOKEN = "test-internal-token"
BODY = {
    "application_id": 7,
    "applicant_id": 99,
    "name": "Jane Doe",
    "dob": "1970-01-01",
    "ssn": "412-55-9981",
    "address": "10 Main St",
    "entity_type": None,
}


def _no_db(monkeypatch):
    def _explode(*a, **k):
        raise AssertionError("no DB write for an unauthorized CIP check")

    monkeypatch.setattr(kyc_router.db, "query", _explode)


def test_kyc_check_403_without_internal_header(monkeypatch):
    monkeypatch.setattr(kyc_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    _no_db(monkeypatch)
    resp = TestClient(app, raise_server_exceptions=False).post("/kyc/check", json=BODY)
    assert resp.status_code == 403


def test_kyc_check_403_with_wrong_header(monkeypatch):
    monkeypatch.setattr(kyc_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    _no_db(monkeypatch)
    resp = TestClient(app, raise_server_exceptions=False).post(
        "/kyc/check", json=BODY, headers={"X-Internal-Service": "wrong"}
    )
    assert resp.status_code == 403


def test_kyc_check_fails_closed_when_token_unset(monkeypatch):
    monkeypatch.setattr(kyc_router.config, "INTERNAL_SERVICE_TOKEN", "")
    _no_db(monkeypatch)
    resp = TestClient(app, raise_server_exceptions=False).post(
        "/kyc/check", json=BODY, headers={"X-Internal-Service": ""}
    )
    assert resp.status_code == 503


def test_kyc_check_allows_correct_header(monkeypatch):
    monkeypatch.setattr(kyc_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    monkeypatch.setattr(kyc_router.db, "query", lambda *a, **k: [{"id": 1}])
    resp = TestClient(app).post(
        "/kyc/check", json=BODY, headers={"X-Internal-Service": TOKEN}
    )
    assert resp.status_code == 200
