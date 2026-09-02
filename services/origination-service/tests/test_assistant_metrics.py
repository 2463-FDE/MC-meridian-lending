"""The read side of `assistant_runs`: what the aggregate refuses, and what it omits.

`assistant_runs.db.query` is stubbed throughout. The claim under test is the route's
contract -- who may call it, what reaches the interval cast, and which columns can appear
in the response -- not whether Postgres groups correctly, which driving a real database
would re-test and which `assistant-telemetry-gate` already covers for the write path.
"""

import pytest
from fastapi.testclient import TestClient

from app import assistant_runs, main

OFFICER = {"X-User-Role": "underwriter"}

# Spelled out rather than read off `assistant_runs.WINDOWS` at import time. A
# parametrize over the module attribute makes the whole file fail to COLLECT when the
# attribute is absent, and a collection error is a red that proves only "this name is
# new" -- it would hide whether the 403 and the 422 fail for their own reasons. Kept
# honest by test_the_allowlist_is_the_one_the_module_enforces below.
WINDOWS = ("1 day", "7 days", "30 days")


def _rows(monkeypatch, rows, captured=None):
    def _query(sql, params=None):
        if captured is not None:
            captured.append((sql, params))
        return rows

    monkeypatch.setattr(assistant_runs.db, "query", _query)
    return TestClient(main.app)


def _group(**overrides):
    row = {
        "task": "explain",
        "http_status": 200,
        "refusal_code": None,
        "outcome": "approve",
        "policy_band": "approve",
        "runs": 3,
        "p50_ms": 800,
        "p95_ms": 1200,
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize("role", [None, "csr", "borrower", "", "underwriterr"])
def test_a_non_officer_is_refused(monkeypatch, role):
    """The gateway proxies GET /los/{path:path} anonymously and enforces no role authz on
    it, so the route body is the only gate. `role=None` is that anonymous caller."""
    client = _rows(monkeypatch, [])
    headers = {} if role is None else {"X-User-Role": role}
    resp = client.get("/assistant/metrics", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "officer role required"


def test_an_officer_is_served(monkeypatch):
    client = _rows(monkeypatch, [_group()])
    resp = client.get("/assistant/metrics", headers=OFFICER)
    assert resp.status_code == 200
    assert resp.json()["recorded_runs"] == 3


def test_the_response_carries_no_application_id_and_no_trace_id(monkeypatch):
    """The test that stops "just add the app id for debugging" from turning an aggregate
    into rows linkable to a customer. Both locks are asserted: the SELECT never asks for
    either column, AND the response model drops them if a later SELECT does."""
    captured = []
    leaky = _group(application_id=42, trace_id="9f3c-run-id")
    client = _rows(monkeypatch, [leaky], captured)
    resp = client.get("/assistant/metrics", headers=OFFICER)
    assert resp.status_code == 200

    sql, _ = captured[0]
    assert "application_id" not in sql
    assert "trace_id" not in sql

    body = resp.json()
    rendered = str(body)
    assert "application_id" not in rendered
    assert "trace_id" not in rendered
    assert "42" not in str(body["groups"][0])


@pytest.mark.parametrize(
    "window",
    ["8 days", "7 days'; DROP TABLE assistant_runs; --", "", "1 year", "7days"],
)
def test_an_off_allowlist_window_is_refused_and_never_reaches_the_cast(
    monkeypatch, window
):
    """`window` is the route's one injection surface: it reaches a Postgres `interval`
    cast. Refused with the vocabulary, never silently defaulted -- a fall back to
    "7 days" would answer a question the officer did not ask."""
    captured = []
    client = _rows(monkeypatch, [], captured)
    resp = client.get("/assistant/metrics", params={"window": window}, headers=OFFICER)
    assert resp.status_code == 422
    assert "unknown window" in resp.json()["detail"]
    assert captured == []


def test_the_allowlist_is_the_one_the_module_enforces():
    """Binds the local copy above to the module's own tuple, so widening one without the
    other is a failure rather than a test that quietly stops covering the new value."""
    assert assistant_runs.WINDOWS == WINDOWS


@pytest.mark.parametrize("window", WINDOWS)
def test_each_allowlisted_window_is_bound_as_a_parameter(monkeypatch, window):
    captured = []
    client = _rows(monkeypatch, [], captured)
    resp = client.get("/assistant/metrics", params={"window": window}, headers=OFFICER)
    assert resp.status_code == 200
    sql, params = captured[0]
    # Bound, not interpolated: the window is never a substring of the statement.
    assert window not in sql
    assert params == (window,)


def test_the_refusal_rate_counts_refusals_among_recorded_runs(monkeypatch):
    client = _rows(
        monkeypatch,
        [
            _group(runs=3),
            _group(
                runs=1, http_status=404, refusal_code="never_decisioned", outcome=None
            ),
        ],
    )
    body = client.get("/assistant/metrics", headers=OFFICER).json()
    assert body["recorded_runs"] == 4
    assert body["refusals_among_recorded_runs"] == 1
    assert body["refusal_rate_among_recorded_runs"] == 0.25


def test_an_empty_window_reports_no_rate_rather_than_zero(monkeypatch):
    """A zero rate over a zero denominator is a claim about a population nobody observed --
    and `record()` swallowing its writes means an empty read is as likely a telemetry
    outage as a quiet week."""
    client = _rows(monkeypatch, [])
    body = client.get("/assistant/metrics", headers=OFFICER).json()
    assert body["recorded_runs"] == 0
    assert body["refusal_rate_among_recorded_runs"] is None


def test_the_aggregate_raises_where_the_write_swallows(monkeypatch):
    """`record()` swallows so a telemetry write can never 500 an officer's answer. A read
    has no such duty, and a swallowed read returns a well-formed zero from a query that
    failed -- indistinguishable from a quiet week."""

    def _explode(sql, params=None):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(assistant_runs.db, "query", _explode)
    with pytest.raises(RuntimeError):
        assistant_runs.aggregate("7 days")


def test_the_module_refuses_an_off_allowlist_window_for_every_caller(monkeypatch):
    """The route answers 422; this is the invariant underneath it, so an in-process caller
    added later cannot reach the interval cast with an arbitrary string."""
    monkeypatch.setattr(
        assistant_runs.db, "query", lambda sql, params=None: pytest.fail("query ran")
    )
    with pytest.raises(ValueError):
        assistant_runs.aggregate("1 year")
