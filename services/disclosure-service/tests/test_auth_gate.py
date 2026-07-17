"""POST /offers is internal-only (PR review).

The offer write persists a TILA/Reg-Z disclosure (offers row) from caller-supplied
inputs and is reachable through the gateway's anonymous /disclosure proxy. An external
caller must be refused before any offer is built or persisted.
"""

from fastapi.testclient import TestClient

from app.main import app
from app.routers import offers as offers_router

TOKEN = "test-internal-token"
BODY = {"application_id": 7, "principal": 15000, "term_months": 36, "annual_rate": 7.99}


def _no_db(monkeypatch):
    def _explode(*a, **k):
        raise AssertionError("no DB write for an unauthorized offer create")

    monkeypatch.setattr(offers_router.db, "query", _explode)


def test_offers_403_without_internal_header(monkeypatch):
    monkeypatch.setattr(offers_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    _no_db(monkeypatch)
    resp = TestClient(app, raise_server_exceptions=False).post("/offers", json=BODY)
    assert resp.status_code == 403


def test_offers_403_with_wrong_header(monkeypatch):
    monkeypatch.setattr(offers_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    _no_db(monkeypatch)
    resp = TestClient(app, raise_server_exceptions=False).post(
        "/offers", json=BODY, headers={"X-Internal-Service": "wrong"}
    )
    assert resp.status_code == 403


def test_offers_fails_closed_when_token_unset(monkeypatch):
    monkeypatch.setattr(offers_router.config, "INTERNAL_SERVICE_TOKEN", "")
    _no_db(monkeypatch)
    resp = TestClient(app, raise_server_exceptions=False).post(
        "/offers", json=BODY, headers={"X-Internal-Service": ""}
    )
    assert resp.status_code == 503


def test_offers_allows_correct_header(monkeypatch):
    monkeypatch.setattr(offers_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    monkeypatch.setattr(offers_router.db, "query", lambda *a, **k: [{"id": 1}])
    resp = TestClient(app).post(
        "/offers", json=BODY, headers={"X-Internal-Service": TOKEN}
    )
    assert resp.status_code == 200
