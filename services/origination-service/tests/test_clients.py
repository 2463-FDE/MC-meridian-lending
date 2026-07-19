"""Internal service-to-service header forwarding (PR review).

The decision-record read is gated behind the X-Internal-Service secret, so the
record fix only holds if origination actually attaches that header on its outbound
calls. The behavioural suites stub clients.get/post, so this locks the forwarding
itself with a real httpx transport — a regression that drops the header (which would
make decision-service answer 503 and break the assistant) fails here.
"""

import httpx

from app import clients


def _capture_with_token(monkeypatch, token, call):
    """Run `call(base, path)` against a mock transport; return the request headers."""
    monkeypatch.setattr(clients, "INTERNAL_SERVICE_TOKEN", token)
    captured = {}

    def handler(request):
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    def _fake_get(url, timeout=None, headers=None):
        with httpx.Client(transport=transport) as c:
            return c.get(url, headers=headers)

    def _fake_post(url, json=None, timeout=None, headers=None):
        with httpx.Client(transport=transport) as c:
            return c.post(url, json=json, headers=headers)

    monkeypatch.setattr(clients.httpx, "get", _fake_get)
    monkeypatch.setattr(clients.httpx, "post", _fake_post)
    call("http://decision:8004", "/decisions/5/record")
    return captured["headers"]


def test_get_forwards_internal_service_header(monkeypatch):
    headers = _capture_with_token(
        monkeypatch, "secret-tok", lambda b, p: clients.get(b, p)
    )
    assert headers.get("x-internal-service") == "secret-tok"


def test_post_forwards_internal_service_header(monkeypatch):
    headers = _capture_with_token(
        monkeypatch, "secret-tok", lambda b, p: clients.post(b, p, {"x": 1})
    )
    assert headers.get("x-internal-service") == "secret-tok"


def test_no_internal_header_when_token_unset(monkeypatch):
    # Token unset: send no header at all (not an empty one) so the downstream guard
    # fails closed rather than being handed a blank identity to compare.
    headers = _capture_with_token(monkeypatch, "", lambda b, p: clients.get(b, p))
    assert "x-internal-service" not in headers
