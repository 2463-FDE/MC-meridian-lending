"""The /los proxy forwards only GET and POST (services/gateway/app/main.py).

PR regression: the monthly_debt remediation endpoint was first written as
PATCH /applications/{id}/monthly-debt, which is unreachable through the product
front door because the gateway does not proxy PATCH — so a legacy NULL-debt row
could never be cleared through the gateway. The endpoint is now POST; this test
locks the constraint that any LOS write must use a method the gateway proxies, so
a future PATCH endpoint on origination re-surfaces the gap here instead of shipping.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_los_proxy_rejects_patch():
    # PATCH is refused at the routing layer (405) before any downstream forward,
    # so this needs no live origination service.
    resp = client.patch("/los/applications/1/monthly-debt", json={"monthly_debt": 450})
    assert resp.status_code == 405


def test_los_route_allows_get_and_post_only():
    route = next(r for r in app.routes if getattr(r, "path", "") == "/los/{path:path}")
    assert route.methods == {"GET", "POST"}


# --- Trust-header stripping (PR review) ---------------------------------------------
#
# Downstream services trust X-User-* / X-Internal-Service as identity, so the gateway
# must be their ONLY source: an external client that sends them itself must have them
# dropped before the request is forwarded, or it can forge officer/internal identity.


class _FakeResp:
    status_code = 200

    def json(self):
        return {"ok": True}


def _capture_forwarded_headers(monkeypatch):
    """Replace the gateway's httpx.AsyncClient with a fake that records the headers it
    would forward downstream, and returns without a network call."""
    from app import main

    captured = {}

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, content=None, headers=None, params=None):
            captured["headers"] = {k.lower(): v for k, v in (headers or {}).items()}
            return _FakeResp()

    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    return captured


def test_anonymous_client_cannot_inject_trust_headers(monkeypatch):
    captured = _capture_forwarded_headers(monkeypatch)
    resp = client.get(
        "/los/applications/1",
        headers={
            "X-User-Role": "admin",
            "X-User-Id": "999",
            "X-Internal-Service": "sneaky-guess",
        },
    )
    assert resp.status_code == 200
    fwd = captured["headers"]
    # None of the client-supplied trust headers survive on the anonymous path.
    assert "x-user-role" not in fwd
    assert "x-user-id" not in fwd
    assert "x-internal-service" not in fwd


def test_anonymous_post_to_decision_cannot_carry_internal_token(monkeypatch):
    # PR review: POST /decisions is internal-only in decision-service. A client that
    # POSTs to /decision/decisions with a guessed X-Internal-Service must have it
    # stripped, so the forwarded request reaches decision-service unauthenticated and
    # is refused there (403) — no regulated decision event can be forged via the proxy.
    captured = _capture_forwarded_headers(monkeypatch)
    resp = client.post(
        "/decision/decisions",
        json={"application_id": 12, "annual_income": 0},
        headers={"X-Internal-Service": "guessed-secret"},
    )
    assert resp.status_code == 200  # proxy itself forwards; auth is enforced downstream
    assert "x-internal-service" not in captured["headers"]


def test_session_role_overrides_client_supplied_role(monkeypatch):
    # An authenticated caller who ALSO sends X-User-Role: admin must be forwarded with
    # the role from their session, never the value they injected.
    from app import auth

    monkeypatch.setattr(
        auth, "get_session", lambda token: {"id": 7, "role": "underwriter"}
    )
    captured = _capture_forwarded_headers(monkeypatch)
    resp = client.get(
        "/los/applications/1",
        headers={"Authorization": "Bearer x", "X-User-Role": "admin"},
    )
    assert resp.status_code == 200
    assert captured["headers"]["x-user-role"] == "underwriter"
    assert captured["headers"]["x-user-id"] == "7"
