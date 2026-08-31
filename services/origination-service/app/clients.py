"""HTTP clients for the extracted KYC / decision / disclosure microservices.

Origination (LOS) used to run CIP, decisioning, and offer/disclosure in-process. Those
were extracted into standalone services; this module is the thin httpx seam that replaces
the old direct function calls. Base URLs come from config (env-driven) with the docker
network http://<svc>:<port> defaults.

Each downstream (KYC/decision/disclosure) gets its own in-process circuit breaker, keyed
by base_url, so a sustained outage in one doesn't make every request queue behind a fresh
30s timeout.
"""

import threading
import time

import httpx

from .config import (  # noqa: F401  (re-exported)
    DECISION_URL,
    DISCLOSURE_URL,
    INTERNAL_SERVICE_TOKEN,
    KYC_URL,
)
from .logging_config import get_logger

log = get_logger("clients")

_TIMEOUT = 30.0

_BREAKER_FAILURE_THRESHOLD = 5
_BREAKER_COOLDOWN_SECONDS = 30.0

_breaker_lock = threading.Lock()
_breaker_state: dict[str, dict] = {}


class CircuitOpenError(httpx.HTTPError):
    """Raised in place of a network call when a downstream's breaker is open."""


def _breaker_for(base_url: str) -> dict:
    return _breaker_state.setdefault(
        base_url, {"state": "closed", "failures": 0, "opened_at": 0.0}
    )


def _breaker_before_call(base_url: str) -> None:
    """Fail fast (no network call) while a breaker is open and its cooldown hasn't
    elapsed. Once cooldown elapses, admit exactly one caller through to test recovery
    (half-open); every other caller fails fast until that probe closes or reopens the
    circuit — otherwise a recovering (or still-dead) downstream takes a concurrent
    burst instead of a single probe."""
    with _breaker_lock:
        b = _breaker_for(base_url)
        if b["state"] == "closed":
            return
        if b["state"] == "half_open":
            raise CircuitOpenError(
                f"circuit half-open probe in progress for {base_url}"
            )
        if time.monotonic() - b["opened_at"] < _BREAKER_COOLDOWN_SECONDS:
            raise CircuitOpenError(f"circuit open for {base_url}")
        b["state"] = "half_open"
        log.warning("breaker state_change=open->half_open downstream=%s", base_url)


def _breaker_after_call(base_url: str, *, ok: bool) -> None:
    with _breaker_lock:
        b = _breaker_for(base_url)
        if ok:
            if b["state"] != "closed":
                log.warning(
                    "breaker state_change=%s->closed downstream=%s",
                    b["state"],
                    base_url,
                )
            b["state"] = "closed"
            b["failures"] = 0
            return
        if b["state"] == "half_open":
            b["state"] = "open"
            b["opened_at"] = time.monotonic()
            log.warning("breaker state_change=half_open->open downstream=%s", base_url)
            return
        b["failures"] += 1
        if b["failures"] >= _BREAKER_FAILURE_THRESHOLD:
            b["state"] = "open"
            b["opened_at"] = time.monotonic()
            log.warning(
                "breaker state_change=closed->open downstream=%s failures=%d",
                base_url,
                b["failures"],
            )


def _with_breaker(base_url: str, make_request):
    """Run `make_request()` behind the breaker for base_url. A downstream 4xx is a
    legitimate response (the service is up and answered) and does not count as a
    breaker failure; a timeout/connection error or a 5xx does.

    `post()` raises on non-2xx, so its failures surface as `httpx.HTTPStatusError`
    below. `get()`/`post_raw()` return the raw response instead of raising, so a 5xx
    from either must still be classified here — checking `resp.status_code` on the
    no-exception path, not just `True`."""
    _breaker_before_call(base_url)
    try:
        resp = make_request()
    except httpx.HTTPStatusError as exc:
        _breaker_after_call(base_url, ok=exc.response.status_code < 500)
        raise
    except httpx.HTTPError:
        _breaker_after_call(base_url, ok=False)
        raise
    _breaker_after_call(base_url, ok=resp.status_code < 500)
    return resp


def _internal_headers() -> dict:
    """Identify these calls as internal service-to-service so downstream internal-only
    routes (decision-service record read) accept them. Empty when the token is unset —
    the downstream route then fails closed rather than trusting an empty header."""
    return (
        {"X-Internal-Service": INTERNAL_SERVICE_TOKEN} if INTERNAL_SERVICE_TOKEN else {}
    )


def post(base_url: str, path: str, payload: dict) -> dict:
    """POST JSON to a downstream service, raise on non-2xx, return the decoded body."""

    def make_request():
        resp = httpx.post(
            f"{base_url}{path}",
            json=payload,
            timeout=_TIMEOUT,
            headers=_internal_headers(),
        )
        resp.raise_for_status()
        return resp

    return _with_breaker(base_url, make_request).json()


def post_raw(base_url: str, path: str, payload: dict) -> httpx.Response:
    """POST and return the raw response, so a caller can forward a downstream 4xx.

    `post()` raises on non-2xx, which turns "the service refused this" into an exception
    indistinguishable from "the service is down". The disclosure lifecycle needs the
    difference: an illegal transition is an answer, not an outage.
    """
    return _with_breaker(
        base_url,
        lambda: httpx.post(
            f"{base_url}{path}",
            json=payload,
            timeout=_TIMEOUT,
            headers=_internal_headers(),
        ),
    )


def get(base_url: str, path: str) -> httpx.Response:
    """GET a downstream service; return the raw response so callers can branch on status
    (e.g. forward a 404 instead of treating it as a 500)."""
    return _with_breaker(
        base_url,
        lambda: httpx.get(
            f"{base_url}{path}", timeout=_TIMEOUT, headers=_internal_headers()
        ),
    )
