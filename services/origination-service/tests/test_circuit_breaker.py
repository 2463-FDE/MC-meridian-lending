"""Circuit breaker around the KYC/decision/disclosure hops (clients.py).

Without a breaker, a downstream that is down still pays a fresh 30s timeout on every
request (docs/handoffs/2026-08-31-audit-medium-term.md item 7). These tests prove the
breaker trips after N consecutive failures (fail-fast, no network call), then recovers
via a half-open probe, and that a downstream 4xx (the service is up and answered) never
counts as a breaker failure.
"""

import threading
import time

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


def test_get_5xx_trips_breaker_without_raising(monkeypatch):
    """`get()` returns the raw response instead of raising on a 5xx, so `_with_breaker`
    must classify it off `resp.status_code`, not off "no exception was raised"."""
    base = "http://kyc-service:8003"

    def unavailable_get(url, **kwargs):
        return httpx.Response(
            503, json={"detail": "unavailable"}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(clients.httpx, "get", unavailable_get)

    for _ in range(clients._BREAKER_FAILURE_THRESHOLD):
        resp = clients.get(base, "/health")
        assert resp.status_code == 503

    assert clients._breaker_state[base]["state"] == "open"


def test_post_raw_5xx_trips_breaker_without_raising(monkeypatch):
    """Same defect as above, for `post_raw()` — used by the disclosure lifecycle proxy
    and the disclosure-coordinator's internal POSTs, both of which classify the 4xx/5xx
    split themselves after the breaker has already recorded the outcome."""
    base = "http://disclosure-service:8005"

    def unavailable_post(url, **kwargs):
        return httpx.Response(
            503, json={"detail": "unavailable"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(clients.httpx, "post", unavailable_post)

    for _ in range(clients._BREAKER_FAILURE_THRESHOLD):
        resp = clients.post_raw(base, "/offers", {})
        assert resp.status_code == 503

    assert clients._breaker_state[base]["state"] == "open"


def test_half_open_admits_exactly_one_concurrent_probe(monkeypatch):
    """Once cooldown elapses, concurrent callers must not all sail through as
    half-open — only the first to acquire the breaker lock probes; every other
    concurrent caller fails fast with CircuitOpenError."""
    base = "http://kyc-service:8003"
    calls = {"n": 0}
    calls_lock = threading.Lock()

    def slow_ok_get(*args, **kwargs):
        with calls_lock:
            calls["n"] += 1
        time.sleep(0.05)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(clients.httpx, "get", slow_ok_get)

    clients._breaker_state[base] = {
        "state": "open",
        "failures": clients._BREAKER_FAILURE_THRESHOLD,
        "opened_at": 0.0,
    }

    results = []
    results_lock = threading.Lock()

    def worker():
        try:
            clients.get(base, "/health")
            outcome = "ok"
        except clients.CircuitOpenError:
            outcome = "blocked"
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls["n"] == 1
    assert results.count("ok") == 1
    assert results.count("blocked") == 19
