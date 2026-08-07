"""Offer routes are internal-only (PR review).

POST /offers persists a TILA/Reg-Z disclosure (offers row) from caller-supplied inputs;
GET /applications/{id}/offer discloses APR/finance charge/payment/schedule for an
enumerable app id. Both are reachable through the gateway's anonymous /disclosure proxy,
so an external caller must be refused — reads must go through the origination
/los/applications/{id}/offer route that enforces owner/officer/token authz.
"""

from fastapi.testclient import TestClient

from app.database import get_session
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
    monkeypatch.setattr(
        offers_router.db,
        "query",
        lambda *a, **k: [
            {
                "id": 1,
                "apr": 7.99,
                "finance_charge": 0.0,
                "monthly_payment": 0.0,
                "amount_financed": 0.0,
                "total_of_payments": 0.0,
            }
        ],
    )
    resp = TestClient(app).post(
        "/offers", json=BODY, headers={"X-Internal-Service": TOKEN}
    )
    assert resp.status_code == 200


class _ExplodingSession:
    """Stand-in DB session whose query fails — proves the gate rejects before any read."""

    def scalar(self, *a, **k):
        raise AssertionError("no offer read for an unauthorized caller")

    def execute(self, *a, **k):
        raise AssertionError("no provenance read for an unauthorized caller")


def _no_read():
    app.dependency_overrides[get_session] = lambda: _ExplodingSession()


def test_get_offer_403_without_internal_header(monkeypatch):
    monkeypatch.setattr(offers_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    _no_read()
    resp = TestClient(app, raise_server_exceptions=False).get("/applications/7/offer")
    app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_get_offer_403_with_wrong_header(monkeypatch):
    monkeypatch.setattr(offers_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    _no_read()
    resp = TestClient(app, raise_server_exceptions=False).get(
        "/applications/7/offer", headers={"X-Internal-Service": "wrong"}
    )
    app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_get_offer_fails_closed_when_token_unset(monkeypatch):
    monkeypatch.setattr(offers_router.config, "INTERNAL_SERVICE_TOKEN", "")
    _no_read()
    resp = TestClient(app, raise_server_exceptions=False).get(
        "/applications/7/offer", headers={"X-Internal-Service": ""}
    )
    app.dependency_overrides.clear()
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# The provenance read (spec D3). It returns the disclosed APR and the applicant id, so it
# sits behind the same gate as the offer routes — the /disclosure/* proxy is anonymous.
# ---------------------------------------------------------------------------


def test_provenance_403_without_internal_header(monkeypatch):
    monkeypatch.setattr(offers_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    _no_read()
    resp = TestClient(app, raise_server_exceptions=False).get(
        "/disclosures/5/provenance"
    )
    app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_provenance_403_with_wrong_header(monkeypatch):
    monkeypatch.setattr(offers_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    _no_read()
    resp = TestClient(app, raise_server_exceptions=False).get(
        "/disclosures/5/provenance", headers={"X-Internal-Service": "wrong"}
    )
    app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_provenance_fails_closed_when_token_unset(monkeypatch):
    monkeypatch.setattr(offers_router.config, "INTERNAL_SERVICE_TOKEN", "")
    _no_read()
    resp = TestClient(app, raise_server_exceptions=False).get(
        "/disclosures/5/provenance", headers={"X-Internal-Service": ""}
    )
    app.dependency_overrides.clear()
    assert resp.status_code == 503


def test_every_route_but_health_is_gated():
    """The `offer-guard-gate` premise, asserted structurally rather than per route.

    Enumerating routes one at a time is how the premise quietly stops being true: someone
    adds a route, forgets the guard, and every existing test still passes. This fails the
    moment a new endpoint on this service does not take the internal-service header.
    """
    import inspect

    ungated = []
    for route in app.routes:
        path = getattr(route, "path", "")
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or path in ("/health", "/openapi.json", "/docs", "/redoc"):
            continue
        if path.startswith("/docs") or path.startswith("/redoc"):
            continue
        if "x_internal_service" not in inspect.signature(endpoint).parameters:
            ungated.append(path)
    assert ungated == [], f"routes missing the internal-caller gate: {ungated}"
