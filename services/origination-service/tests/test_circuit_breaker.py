"""Circuit breaker around the KYC/decision/disclosure hops (clients.py).

Without a breaker, a downstream that is down still pays a fresh 30s timeout on every
request (docs/handoffs/2026-08-31-audit-medium-term.md item 7). These tests prove the
breaker trips after N consecutive failures (fail-fast, no network call), then recovers
via a half-open probe, and that a downstream 4xx (the service is up and answered) never
counts as a breaker failure.
"""

import httpx
import pytest

from app import clients


@pytest.fixture(autouse=True)
def _reset_breaker_state():
    clients._breaker_state.clear()
    yield
    clients._breaker_state.clear()


def test_breaker_trips_after_threshold_and_fails_fast(monkeypatch):
    calls = {"n": 0}

    def counting_get(*args, **kwargs):
        calls["n"] += 1
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(clients.httpx, "get", counting_get)

    base = "http://kyc-service:8003"
    for _ in range(clients._BREAKER_FAILURE_THRESHOLD):
        with pytest.raises(httpx.ConnectError):
            clients.get(base, "/health")

    assert calls["n"] == clients._BREAKER_FAILURE_THRESHOLD
    assert clients._breaker_state[base]["state"] == "open"

    # Next call must fail fast: typed error, no network attempt.
    with pytest.raises(clients.CircuitOpenError):
        clients.get(base, "/health")
    assert calls["n"] == clients._BREAKER_FAILURE_THRESHOLD


def test_breaker_closes_after_successful_half_open_probe(monkeypatch):
    base = "http://decision-service:8004"

    def ok_get(*args, **kwargs):
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(clients.httpx, "get", ok_get)

    # Open, with cooldown already elapsed (opened_at far in the past).
    clients._breaker_state[base] = {
        "state": "open",
        "failures": clients._BREAKER_FAILURE_THRESHOLD,
        "opened_at": 0.0,
    }

    resp = clients.get(base, "/health")
    assert resp.status_code == 200
    assert clients._breaker_state[base]["state"] == "closed"
    assert clients._breaker_state[base]["failures"] == 0


def test_breaker_reopens_on_failed_half_open_probe(monkeypatch):
    base = "http://disclosure-service:8005"

    def failing_get(*args, **kwargs):
        raise httpx.ConnectError("still down")

    monkeypatch.setattr(clients.httpx, "get", failing_get)

    clients._breaker_state[base] = {
        "state": "open",
        "failures": clients._BREAKER_FAILURE_THRESHOLD,
        "opened_at": 0.0,
    }

    with pytest.raises(httpx.ConnectError):
        clients.get(base, "/health")
    assert clients._breaker_state[base]["state"] == "open"


def test_downstream_4xx_does_not_trip_breaker(monkeypatch):
    base = "http://decision-service:8004"

    def bad_request_post(url, **kwargs):
        return httpx.Response(
            422, json={"detail": "invalid"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(clients.httpx, "post", bad_request_post)

    for _ in range(clients._BREAKER_FAILURE_THRESHOLD):
        with pytest.raises(httpx.HTTPStatusError):
            clients.post(base, "/decisions", {})

    assert clients._breaker_state[base]["state"] == "closed"
