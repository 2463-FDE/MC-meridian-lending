"""The /los proxy forwards only GET and POST (services/gateway/app/main.py).

PR regression: the monthly_debt remediation endpoint was first written as
PATCH /applications/{id}/monthly-debt, which is unreachable through the product
front door because the gateway does not proxy PATCH — so a legacy NULL-debt row
could never be cleared through the gateway. The endpoint is now POST; this test
locks the constraint that any LOS write must use a method the gateway proxies, so
a future PATCH endpoint on origination re-surfaces the gap here instead of shipping.
"""

from fastapi.testclient import TestClient

from app.main import app  # noqa: E402

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
    def __init__(self, body=None, status_code=200):
        self._body = {"ok": True} if body is None else body
        self.status_code = status_code

    def json(self):
        return self._body


def _capture_forwarded_headers(monkeypatch, resp_body=None):
    """Replace the gateway's httpx.AsyncClient with a fake that records the headers it
    would forward downstream and returns resp_body (default {"ok": True}) without a
    network call."""
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
            return _FakeResp(body=resp_body)

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


def test_los_proxy_forwards_idempotency_key(monkeypatch):
    # PR review (front-door reachability): the decision idempotency guarantee only works
    # if the client's Idempotency-Key survives the proxy. The gateway strips trust
    # headers but must FORWARD benign ones — a stripped Idempotency-Key would silently
    # turn every borrower/officer retry into a fresh bureau pull + duplicate event.
    captured = _capture_forwarded_headers(monkeypatch)
    resp = client.post(
        "/los/applications/1/decision",
        headers={"Idempotency-Key": "los-decision-1"},
    )
    assert resp.status_code == 200
    assert captured["headers"].get("idempotency-key") == "los-decision-1"


def test_los_proxy_strips_client_supplied_application_token(monkeypatch):
    # PR #7 review: the continuation token is no longer a client-supplied header. It lives
    # server-side (Redis) behind the HttpOnly resume cookie, and the gateway is its ONLY
    # source (injected from the resume session below). A client that sends its own
    # X-Application-Token -- e.g. a stolen token, or a script probing -- must have it
    # stripped, exactly like X-User-*/X-Internal-Service, so it cannot authorize by header.
    captured = _capture_forwarded_headers(monkeypatch)
    resp = client.post(
        "/los/applications/1/decision",
        headers={"X-Application-Token": "tok-xyz"},
    )
    assert resp.status_code == 200
    assert "x-application-token" not in captured["headers"]


# --- anonymous resume via server-side session + HttpOnly cookie (PR #7 review) -------
#
# The continuation token must never be browser-readable. The gateway stashes it in Redis
# keyed by an opaque id, hands the browser an HttpOnly cookie holding only that id, strips
# the raw token from the submit body, and re-injects the token downstream from the session.


def _fake_resume_store(monkeypatch):
    """In-memory stand-in for the Redis-backed resume session (tests have no Redis).
    Also stubs the submit-time Redis reachability pre-check to healthy."""
    from app import auth, main

    store = {}
    calls = {"cleared": []}

    def _create(app_id, token):
        sid = f"sid-{app_id}"
        store[sid] = {"app_id": app_id, "token": token}
        return sid

    def _resolve(sid):
        return store.get(sid)

    def _clear(sid):
        calls["cleared"].append(sid)
        store.pop(sid, None)

    monkeypatch.setattr(auth, "create_resume_session", _create)
    monkeypatch.setattr(auth, "resolve_resume", _resolve)
    monkeypatch.setattr(auth, "clear_resume", _clear)
    monkeypatch.setattr(main.config, "redis_reachable", lambda *a, **k: (True, None))
    return store, calls


def test_submit_stashes_token_server_side_and_sets_httponly_cookie(monkeypatch):
    store, _ = _fake_resume_store(monkeypatch)
    _capture_forwarded_headers(
        monkeypatch,
        resp_body={"app_id": 5, "continuation_token": "raw-tok", "status": "submitted"},
    )
    resp = client.post("/los/applications", json={"name": "Jane", "amount": 15000})
    assert resp.status_code == 200
    # The raw token is stripped from the body the browser sees.
    assert "continuation_token" not in resp.json()
    assert resp.json()["app_id"] == 5
    # It is stored server-side instead.
    assert store["sid-5"] == {"app_id": 5, "token": "raw-tok"}
    # And handed back only as an HttpOnly, SameSite=Strict, path-scoped cookie.
    set_cookie = resp.headers.get("set-cookie", "")
    lc = set_cookie.lower()
    assert "meridian_resume=sid-5" in set_cookie
    assert "httponly" in lc
    assert "samesite=strict" in lc
    assert "path=/los" in lc


def test_resume_cookie_injects_token_downstream_scoped_to_app(monkeypatch):
    _fake_resume_store(monkeypatch)[0]["sid-5"] = {"app_id": 5, "token": "raw-tok"}
    captured = _capture_forwarded_headers(monkeypatch)
    resp = TestClient(app, cookies={"meridian_resume": "sid-5"}).post(
        "/los/applications/5/decision"
    )
    assert resp.status_code == 200
    # The gateway re-attaches the token from the session; the browser never sent it.
    assert captured["headers"].get("x-application-token") == "raw-tok"


def test_resume_cookie_not_injected_for_a_different_application(monkeypatch):
    # Scope: a resume session for app 5 must not authorize app 9 (a cookie for app A cannot
    # drive app B). The token is not injected, so origination denies the anonymous caller.
    _fake_resume_store(monkeypatch)[0]["sid-5"] = {"app_id": 5, "token": "raw-tok"}
    captured = _capture_forwarded_headers(monkeypatch)
    resp = TestClient(app, cookies={"meridian_resume": "sid-5"}).post(
        "/los/applications/9/decision"
    )
    assert resp.status_code == 200
    assert "x-application-token" not in captured["headers"]


def test_submit_refused_before_creating_application_when_redis_down(monkeypatch):
    # PR #7 review: submit commits an application whose only anonymous credential is the
    # Redis resume session. If Redis is down, refuse UP FRONT (503) so origination never
    # creates a stranded application whose one-time token would be discarded.
    from app import main

    monkeypatch.setattr(main.config, "redis_reachable", lambda *a, **k: (False, "down"))
    captured = _capture_forwarded_headers(
        monkeypatch
    )  # would record a forward if it happened
    resp = client.post("/los/applications", json={"name": "Jane", "amount": 15000})
    assert resp.status_code == 503
    # Origination was never called -> no application committed, clean retry.
    assert "headers" not in captured


def test_submit_returns_503_not_500_when_session_write_fails(monkeypatch):
    # PR #7 review: Redis passes the pre-check but the setex then fails (blip in the window).
    # The gateway must not 500 with the raw token silently discarded; it returns a retryable
    # 503 and never sets a resume cookie. (The committed application is inert/officer-
    # reconcilable -- see ADR 0010 consequences.)
    from app import auth, main

    monkeypatch.setattr(main.config, "redis_reachable", lambda *a, **k: (True, None))

    def _boom(*a, **k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(auth, "create_resume_session", _boom)
    _capture_forwarded_headers(
        monkeypatch, resp_body={"app_id": 5, "continuation_token": "raw-tok"}
    )
    resp = client.post("/los/applications", json={"name": "Jane", "amount": 15000})
    assert resp.status_code == 503
    # No resume cookie, and the raw token never reaches the browser body.
    assert "meridian_resume" not in resp.headers.get("set-cookie", "")
    assert "continuation_token" not in resp.json()


def test_anonymous_resume_read_503s_when_redis_down(monkeypatch):
    # Parity with submit (PR #7 review): a Redis outage on a resume read (decision/offer/
    # accept) must not 500 -- an anonymous caller gets a retryable 503, not a stack trace.
    from app import auth

    def _boom(sid):
        raise RuntimeError("redis down")

    monkeypatch.setattr(auth, "resolve_resume", _boom)
    resp = TestClient(app, cookies={"meridian_resume": "sid-5"}).post(
        "/los/applications/5/decision"
    )
    assert resp.status_code == 503


def test_authenticated_resume_read_unaffected_by_redis_down(monkeypatch):
    # An authenticated caller does not need the resume session; a Redis blip resolving a
    # stale cookie must not block them -- they proceed via their login session.
    from app import auth

    monkeypatch.setattr(
        auth, "get_session", lambda token: {"id": 7, "role": "borrower"}
    )

    def _boom(sid):
        raise RuntimeError("redis down")

    monkeypatch.setattr(auth, "resolve_resume", _boom)
    captured = _capture_forwarded_headers(monkeypatch)
    resp = TestClient(app, cookies={"meridian_resume": "sid-5"}).post(
        "/los/applications/5/decision", headers={"Authorization": "Bearer sesh"}
    )
    assert resp.status_code == 200  # session auth carries them; no token injected
    assert "x-application-token" not in captured["headers"]


def test_authenticated_submit_needs_no_resume_session(monkeypatch):
    # An authenticated owner resumes via their login session, not the anonymous resume
    # cookie. Their submit must NOT create a resume session and must NOT be 503'd when Redis
    # is down (the cookie mechanism is anonymous-only). The token is still stripped from the
    # body (they never use it).
    from app import auth, main

    monkeypatch.setattr(
        auth, "get_session", lambda token: {"id": 7, "role": "borrower"}
    )
    monkeypatch.setattr(main.config, "redis_reachable", lambda *a, **k: (False, "down"))

    def _must_not_create(*a, **k):
        raise AssertionError("authenticated submit must not create a resume session")

    monkeypatch.setattr(auth, "create_resume_session", _must_not_create)
    _capture_forwarded_headers(
        monkeypatch, resp_body={"app_id": 5, "continuation_token": "raw-tok"}
    )
    resp = TestClient(app).post(
        "/los/applications",
        json={"name": "Jane", "amount": 15000},
        headers={"Authorization": "Bearer sesh"},
    )
    assert resp.status_code == 200  # not 503 -- Redis is irrelevant to an authed submit
    assert "meridian_resume" not in resp.headers.get("set-cookie", "")
    assert "continuation_token" not in resp.json()


def test_recheck_response_body_never_leaks_the_raw_token(monkeypatch):
    # recheck-kyc echoes the continuation token in its body downstream; the gateway must
    # strip it (like it does at submit) so the browser never receives the raw credential.
    _fake_resume_store(monkeypatch)[0]["sid-5"] = {"app_id": 5, "token": "raw-tok"}
    _capture_forwarded_headers(
        monkeypatch,
        resp_body={"app_id": 5, "status": "submitted", "continuation_token": "raw-tok"},
    )
    resp = TestClient(app, cookies={"meridian_resume": "sid-5"}).post(
        "/los/applications/5/recheck-kyc"
    )
    assert resp.status_code == 200
    assert "continuation_token" not in resp.json()
    assert resp.json()["app_id"] == 5


def test_resume_cookie_injects_token_on_offer_route(monkeypatch):
    # /los/offer carries app_id in the body, not the path, so the gateway injects the
    # session token and origination binds+validates it against body.app_id downstream.
    _fake_resume_store(monkeypatch)[0]["sid-5"] = {"app_id": 5, "token": "raw-tok"}
    captured = _capture_forwarded_headers(monkeypatch)
    resp = TestClient(app, cookies={"meridian_resume": "sid-5"}).post(
        "/los/offer", json={"app_id": 5}
    )
    assert resp.status_code == 200
    assert captured["headers"].get("x-application-token") == "raw-tok"


def test_accept_revokes_resume_session_and_clears_cookie(monkeypatch):
    store, calls = _fake_resume_store(monkeypatch)
    store["sid-5"] = {"app_id": 5, "token": "raw-tok"}
    _capture_forwarded_headers(monkeypatch, resp_body={"loan_id": 77})
    resp = TestClient(app, cookies={"meridian_resume": "sid-5"}).post(
        "/los/applications/5/accept"
    )
    assert resp.status_code == 200
    # Terminal money action: the server-side session is revoked and the cookie cleared.
    assert calls["cleared"] == ["sid-5"]
    set_cookie = resp.headers.get("set-cookie", "")
    assert "meridian_resume=" in set_cookie
    assert ("Max-Age=0" in set_cookie) or (
        "expires=Thu, 01 Jan 1970" in set_cookie.lower()
    )


def test_anonymous_post_to_offer_cannot_carry_internal_token(monkeypatch):
    # PR review: /los/offer makes origination call disclosure-service with the internal
    # token (a confused-deputy write). A client-supplied X-Internal-Service must be
    # stripped at the gateway so it cannot forge internal identity via the anonymous LOS
    # proxy (the offer's money inputs are separately bound to the stored app server-side).
    captured = _capture_forwarded_headers(monkeypatch)
    resp = client.post(
        "/los/offer",
        json={"app_id": 1},
        headers={"X-Internal-Service": "guessed-secret"},
    )
    assert resp.status_code == 200  # proxy forwards; auth is enforced downstream
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
