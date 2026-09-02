"""The read side of `assistant_runs`: what the aggregate refuses, and what it omits.

The SQLAlchemy session is stubbed throughout. The claim under test is the route's
contract -- who may call it, what reaches the interval cast, which columns can appear in
the response, and what happens on a value the vocabulary does not admit -- not whether
Postgres groups correctly, which driving a real database would re-test and which
`assistant-telemetry-gate` already covers for the write path.
"""

import logging

import pytest
from fastapi.testclient import TestClient

from app import assistant_runs, config, database, main

OFFICER = {"X-User-Role": "underwriter"}

# Spelled out rather than read off `assistant_runs.WINDOWS` at import time. A
# parametrize over the module attribute makes the whole file fail to COLLECT when the
# attribute is absent, and a collection error is a red that proves only "this name is
# new" -- it would hide whether the 403 and the 422 fail for their own reasons. Kept
# honest by test_the_allowlist_is_the_one_the_module_enforces below.
WINDOWS = ("1 day", "7 days", "30 days")


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """Records every statement and its bound parameters.

    A list of (sql, params) in call order, so a test can assert BOTH what reached the
    interval cast and that the statement timeout was set before the aggregate ran.
    """

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params))
        if "FROM assistant_runs" not in sql:
            return _FakeResult([])
        return _FakeResult(self.rows)

    def close(self):
        pass


def _client(monkeypatch, rows, schema_ready=True, not_ready=None):
    """A TestClient whose session yields `rows` and whose readiness rung is stubbed.

    `database_reachable` is stubbed because the route calls it before the query: without
    this every test would open a Postgres connection, and the route's own 503 path is
    what test_an_unmigrated_volume_is_refused_with_the_rungs_reason covers instead.
    """
    session = _FakeSession(rows)
    monkeypatch.setattr(config, "database_reachable", lambda: (schema_ready, not_ready))
    main.app.dependency_overrides[database.get_session] = lambda: session
    return TestClient(main.app), session


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    main.app.dependency_overrides.clear()


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


def _aggregate_call(session):
    """The (sql, params) of the aggregate itself, skipping the SET LOCAL before it."""
    return next(call for call in session.calls if "FROM assistant_runs" in call[0])


@pytest.mark.parametrize("role", [None, "csr", "borrower", "", "underwriterr"])
def test_a_non_officer_is_refused(monkeypatch, role):
    """The gateway proxies GET /los/{path:path} anonymously and enforces no role authz on
    it, so the route body is the only gate. `role=None` is that anonymous caller."""
    client, session = _client(monkeypatch, [])
    headers = {} if role is None else {"X-User-Role": role}
    resp = client.get("/assistant/metrics", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "officer role required"
    # Refused before the database is touched at all -- not even the readiness probe.
    assert session.calls == []


def test_an_officer_is_served(monkeypatch):
    client, _ = _client(monkeypatch, [_group()])
    resp = client.get("/assistant/metrics", headers=OFFICER)
    assert resp.status_code == 200
    assert resp.json()["recorded_runs"] == 3


def test_the_response_carries_no_application_id_and_no_trace_id(monkeypatch):
    """The test that stops "just add the app id for debugging" from turning an aggregate
    into rows linkable to a customer. Both locks are asserted: the SELECT never asks for
    either column, AND the response model drops them if a later SELECT does."""
    leaky = _group(application_id=42, trace_id="9f3c-run-id")
    client, session = _client(monkeypatch, [leaky])
    resp = client.get("/assistant/metrics", headers=OFFICER)
    assert resp.status_code == 200

    sql, _ = _aggregate_call(session)
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
    client, session = _client(monkeypatch, [])
    resp = client.get("/assistant/metrics", params={"window": window}, headers=OFFICER)
    assert resp.status_code == 422
    assert "unknown window" in resp.json()["detail"]
    assert [c for c in session.calls if "FROM assistant_runs" in c[0]] == []


def test_the_allowlist_is_the_one_the_module_enforces():
    """Binds the local copy above to the module's own tuple, so widening one without the
    other is a failure rather than a test that quietly stops covering the new value."""
    assert assistant_runs.WINDOWS == WINDOWS


@pytest.mark.parametrize("window", WINDOWS)
def test_each_allowlisted_window_is_bound_as_a_parameter(monkeypatch, window):
    client, session = _client(monkeypatch, [])
    resp = client.get("/assistant/metrics", params={"window": window}, headers=OFFICER)
    assert resp.status_code == 200
    sql, params = _aggregate_call(session)
    # Bound, not interpolated: the window is never a substring of the statement.
    assert window not in sql
    assert params["window"] == window


def test_the_read_is_bounded_by_a_statement_timeout_and_a_row_limit(monkeypatch):
    """The aggregate is a full-window GROUP BY over a table with no retention policy, and
    it now runs on the pooled session rather than the shared autocommit psycopg2
    connection the money paths serialise on. Both bounds are asserted here because
    neither is visible from the response on a small window."""
    client, session = _client(monkeypatch, [_group()])
    assert client.get("/assistant/metrics", headers=OFFICER).status_code == 200

    timeout_sql, timeout_params = session.calls[0]
    assert "SET LOCAL statement_timeout" in timeout_sql
    assert str(assistant_runs.STATEMENT_TIMEOUT_MS) in timeout_sql
    # SET LOCAL, not SET: the session's connection goes back to a pool, and a plain SET
    # would carry this timeout onto whatever request checks it out next.
    assert "SET LOCAL" in timeout_sql
    assert timeout_params is None

    sql, params = _aggregate_call(session)
    assert "LIMIT :row_limit" in sql
    # One more than the cap, so "there were more" comes from the query.
    assert params["row_limit"] == assistant_runs.MAX_GROUPS + 1


def test_a_full_page_reports_truncated_and_serves_the_cap(monkeypatch):
    """`recorded_runs` over a truncated list is a count of part of the window. The flag is
    the only thing that stops it being read as the whole window."""
    rows = [_group(http_status=200 + i) for i in range(assistant_runs.MAX_GROUPS + 1)]
    client, _ = _client(monkeypatch, rows)
    body = client.get("/assistant/metrics", headers=OFFICER).json()
    assert body["truncated"] is True
    assert len(body["groups"]) == assistant_runs.MAX_GROUPS


def test_an_unfilled_page_reports_not_truncated(monkeypatch):
    client, _ = _client(monkeypatch, [_group()])
    assert (
        client.get("/assistant/metrics", headers=OFFICER).json()["truncated"] is False
    )


def test_the_refusal_rate_counts_refusals_among_recorded_runs(monkeypatch):
    client, _ = _client(
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
    client, _ = _client(monkeypatch, [])
    body = client.get("/assistant/metrics", headers=OFFICER).json()
    assert body["recorded_runs"] == 0
    assert body["refusal_rate_among_recorded_runs"] is None


# --- The vocabulary mask ------------------------------------------------------------
#
# `outcome` and `policy_band` are bare TEXT in the schema: only `task` and `refusal_code`
# carry a CHECK. So the response model is the only thing between an unconstrained write
# and an officer's screen.


def test_an_unconstrained_outcome_is_masked_not_served(monkeypatch):
    """The proven leak. `outcome` has no CHECK, so a hand-applied fix, a backfill or a
    restored volume can put anything in it -- and this row was served verbatim, borrower
    name and SSN included, while the field was `str | None`."""
    leak = "Jane Doe SSN 123-45-6789 approved"
    client, _ = _client(monkeypatch, [_group(outcome=leak)])
    body = client.get("/assistant/metrics", headers=OFFICER).json()
    assert leak not in str(body)
    assert "123-45-6789" not in str(body)
    assert body["groups"][0]["outcome"] == assistant_runs.UNRECOGNISED


@pytest.mark.parametrize(
    "field,value",
    [
        ("task", "explain; DROP TABLE assistant_runs"),
        ("refusal_code", "Jane Doe called about her SSN 123-45-6789"),
        ("outcome", "approve pending review of 4111111111111111"),
        ("policy_band", "band: applicant Jane Doe, 555-0142"),
    ],
)
def test_every_coded_column_is_masked_when_off_vocabulary(monkeypatch, field, value):
    """All four in one pass. Two are CHECK-constrained today and two are not, but which
    two is a property of the schema at this moment -- the boundary does not get to depend
    on it."""
    client, _ = _client(monkeypatch, [_group(**{field: value})])
    body = client.get("/assistant/metrics", headers=OFFICER).json()
    assert value not in str(body)
    assert body["groups"][0][field] == assistant_runs.UNRECOGNISED


@pytest.mark.parametrize(
    "field,vocabulary",
    [
        ("task", assistant_runs.TASKS),
        ("refusal_code", assistant_runs.REFUSAL_CODES),
        ("outcome", assistant_runs.OUTCOMES),
        ("policy_band", assistant_runs.POLICY_BANDS),
    ],
)
def test_every_in_vocabulary_value_survives_the_mask(monkeypatch, field, vocabulary):
    """The mask must not over-block: a legitimate code has to come back as itself, or the
    aggregate reports every run as unrecognised and the fix is worse than the leak."""
    for value in vocabulary:
        row = _group(**{field: value})
        if field == "refusal_code":
            row["http_status"] = 404
        client, _ = _client(monkeypatch, [row])
        body = client.get("/assistant/metrics", headers=OFFICER).json()
        assert body["groups"][0][field] == value


def test_a_null_coded_column_stays_null(monkeypatch):
    """NULL means `_charted` left the field off because the run did not carry it. Masking
    it to UNRECOGNISED would report an unconstrained write where there was none."""
    client, _ = _client(
        monkeypatch, [_group(outcome=None, policy_band=None, refusal_code=None)]
    )
    group = client.get("/assistant/metrics", headers=OFFICER).json()["groups"][0]
    assert group["outcome"] is None
    assert group["policy_band"] is None
    assert group["refusal_code"] is None


def test_a_masked_refusal_code_still_counts_as_a_refusal(monkeypatch):
    """The count is taken off the raw row, before the mask. A refusal_code the vocabulary
    does not admit is still a refusal, and masking it must not move that run into the
    served population."""
    client, _ = _client(
        monkeypatch,
        [_group(runs=1), _group(runs=1, http_status=500, refusal_code="who_knows")],
    )
    body = client.get("/assistant/metrics", headers=OFFICER).json()
    assert body["refusals_among_recorded_runs"] == 1
    assert body["groups"][1]["refusal_code"] == assistant_runs.UNRECOGNISED


def test_the_refusal_vocabulary_matches_the_check_constraint():
    """Rebuilds `ck_assistant_runs_refusal_code`'s expected definition from REFUSAL_CODES,
    so the read boundary cannot start masking a code the write path accepts (or admitting
    one it does not). config._ASSISTANT_RUNS_CHECKS is the definition the readiness rung
    compares against a real Postgres, so binding to it binds to the database."""
    codes = ", ".join(f"'{code}'::text" for code in assistant_runs.REFUSAL_CODES)
    expected = config._normalize_constraint_def(
        f"CHECK (((refusal_code IS NULL) OR (refusal_code = ANY (ARRAY[{codes}]))))"
    )
    assert expected == config._ASSISTANT_RUNS_CHECKS["ck_assistant_runs_refusal_code"]


def test_the_task_vocabulary_matches_the_check_constraint():
    tasks = ", ".join(f"'{task}'::text" for task in assistant_runs.TASKS)
    expected = config._normalize_constraint_def(
        f"CHECK ((task = ANY (ARRAY[{tasks}])))"
    )
    assert expected == config._ASSISTANT_RUNS_CHECKS["ck_assistant_runs_task"]


# --- Failure modes ------------------------------------------------------------------


def test_an_unmigrated_volume_is_refused_with_the_rungs_reason(monkeypatch):
    """Without this the SELECT raises UndefinedTable and `unhandled` flattens it to
    {"detail": "internal error"} -- indistinguishable from the database being down or
    from a bug in the route. The rung already knows which it is."""
    client, session = _client(
        monkeypatch,
        [_group()],
        schema_ready=False,
        not_ready="schema_not_ready:assistant_runs",
    )
    resp = client.get("/assistant/metrics", headers=OFFICER)
    assert resp.status_code == 503
    assert "schema_not_ready:assistant_runs" in resp.json()["detail"]
    # Refused before the query, not after it failed.
    assert session.calls == []


class _CaptureHandler(logging.Handler):
    """Collect records emitted on the origination logger. `logging_config.get_logger` sets
    `propagate = False`, so `caplog` reports "nothing was logged" for a line that WAS
    logged -- a false green on exactly the log line under test (same reason
    test_authz.py and test_llm_client.py each carry their own copy)."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_a_row_that_does_not_match_the_model_never_reaches_the_log(monkeypatch):
    """ValidationError's own text embeds `input_value={...}` -- the whole row. `record()`
    logs `exc.__class__.__name__` for exactly this reason; the read path holds the same
    line. The field names are the diagnostic, the values are not."""
    row = _group()
    del row["p95_ms"]
    client, _ = _client(monkeypatch, [row])
    handler = _CaptureHandler()
    logger = logging.getLogger("origination")
    logger.addHandler(handler)
    try:
        resp = client.get("/assistant/metrics", headers=OFFICER)
    finally:
        logger.removeHandler(handler)
    assert resp.status_code == 500
    assert resp.json()["detail"] == "internal error"

    errors = [r for r in handler.records if r.levelno >= logging.ERROR]
    assert errors, "a row the model rejects must be logged"
    logged = "\n".join(r.getMessage() for r in errors)
    assert "ValidationError" in logged
    assert "p95_ms" in logged
    # The row's values, and the shape that carries them, never appear -- and neither does
    # `unhandled`'s own line, which logs %s of the exception and would embed the row.
    assert "input_value" not in logged
    assert "800" not in logged
    assert "unhandled error" not in logged


def test_the_aggregate_raises_where_the_write_swallows(monkeypatch):
    """`record()` swallows so a telemetry write can never 500 an officer's answer. A read
    has no such duty, and a swallowed read returns a well-formed zero from a query that
    failed -- indistinguishable from a quiet week."""

    class _Exploding:
        def execute(self, statement, params=None):
            raise RuntimeError("connection refused")

    with pytest.raises(RuntimeError):
        assistant_runs.aggregate(_Exploding(), "7 days")


def test_the_module_refuses_an_off_allowlist_window_for_every_caller():
    """The route answers 422; this is the invariant underneath it, so an in-process caller
    added later cannot reach the interval cast with an arbitrary string."""

    class _Failing:
        def execute(self, statement, params=None):
            pytest.fail("query ran")

    with pytest.raises(ValueError):
        assistant_runs.aggregate(_Failing(), "1 year")
