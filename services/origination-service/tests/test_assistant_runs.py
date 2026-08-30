"""One assistant_runs row per officer request — served or refused.

The row is written at the ENTRY span and not inside `assistant.run()`, and that is the
whole point of these tests. Every refusal is translated in `main._run_assistant`; by then
`run()` has raised and its own `root.add_metadata` block has not executed. A write placed
inside `run()` would record successes only and drop the refusal population entirely —
which is the half an operator needs, and the half that is invisible today.

`assistant.run` is stubbed throughout. The claim under test is what the route RECORDS,
not what the loop decides; driving a real loop would test the loop again and make these
cases depend on adapter scripting.
"""

import re
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from app import assistant, assistant_runs, main
from app.llm.errors import LLMError

SERVED = {
    "outcome": "deny",
    "record_status": "recorded",
    "policy_band": "deny",
    "narration_validated": True,
    "policy_citations": ["underwriting_guidelines#credit-decisioning"],
    "policy_searches": ["late fee waiver"],
    # Present in the real result and deliberately NOT recorded — the officer-facing
    # summary is prose, and application_id is already a column of its own.
    "summary": "The application was denied: obligations are excessive.",
    "application_id": 42,
}


class _Span:
    def __init__(self):
        self.metadata = {}
        self.trace_id = uuid.uuid4()

    def add_metadata(self, metadata):
        self.metadata.update(metadata or {})


class _Recorder:
    """Stands in for `trace()`, which is a no-op unless LANGSMITH_TRACING is set.

    `trace_id` is populated either way — tracing off means "do not ship spans", not "do
    not build them" — which is exactly why the row can carry it on a volume that exports
    nothing.
    """

    def __init__(self, *a, **k):
        self.span = _Span()

    def __enter__(self):
        return self.span

    def __exit__(self, *exc):
        return False


@pytest.fixture
def rows(monkeypatch):
    """Capture what reaches the database, as (sql, params)."""
    captured = []
    monkeypatch.setattr(main, "trace", _Recorder, raising=False)
    monkeypatch.setattr(
        assistant_runs.db, "query", lambda sql, params=None: captured.append((sql, params))
    )
    return captured


def _columns(sql: str) -> list[str]:
    inner = sql[sql.index("(") + 1 : sql.index(")")]
    return [c.strip() for c in inner.split(",")]


def _row(captured) -> dict:
    assert len(captured) == 1, f"expected exactly one row, got {len(captured)}"
    sql, params = captured[0]
    return dict(zip(_columns(sql), params))


def _serve(monkeypatch, result=None):
    monkeypatch.setattr(assistant, "run", lambda *a, **k: result or dict(SERVED))


def _refuse(monkeypatch, exc):
    def _raise(*a, **k):
        raise exc

    monkeypatch.setattr(assistant, "run", _raise)


def test_a_served_run_is_recorded(rows, monkeypatch):
    _serve(monkeypatch)
    main._run_assistant(42, None, "decision", policy_topic=None)
    row = _row(rows)
    assert row["http_status"] == 200
    assert row["refusal_code"] is None
    assert row["application_id"] == 42
    assert row["task"] == "decision"
    assert row["outcome"] == "deny"
    assert row["record_status"] == "recorded"
    assert row["policy_band"] == "deny"
    assert row["narration_validated"] is True
    # The LISTS are recorded as their lengths, the way the span promotes them.
    assert row["policy_citations"] == 1
    assert row["policy_searches"] == 1
    assert isinstance(row["latency_ms"], int)


REFUSALS = [
    (assistant.ApplicationNeverDecisioned("never decisioned"), 404, "never_decisioned"),
    (assistant.ApplicationNotFound("nope"), 404, "not_found"),
    (assistant.AssistantError("no record"), 502, "assistant_refused"),
    (LLMError("provider down"), 503, "llm_unavailable"),
    (HTTPException(status_code=409, detail="kyc"), 409, "kyc_blocked"),
    (HTTPException(status_code=403, detail="denied"), 403, "refused"),
    (httpx.ConnectError("unreachable"), 503, "downstream_unavailable"),
]


@pytest.mark.parametrize("exc,status,code", REFUSALS)
def test_every_refusal_is_recorded(rows, monkeypatch, exc, status, code):
    """The population `assistant.run()` can never see. Each code is also a value the
    CHECK constraint admits — a branch added without widening it would insert nothing,
    and the write swallows failures, so the row would vanish silently."""
    _refuse(monkeypatch, exc)
    with pytest.raises(HTTPException):
        main._run_assistant(42, None, "explain")
    row = _row(rows)
    assert row["http_status"] == status
    assert row["refusal_code"] == code
    assert row["outcome"] is None


def test_never_decisioned_is_not_recorded_as_not_found(rows, monkeypatch):
    """The load-bearing split. `ApplicationNeverDecisioned` subclasses
    `ApplicationNotFound`, so the officer still gets the same 404 and the same detail —
    but "this id is not an application" and "it is an application nobody has decisioned"
    have opposite remedies, and one refusal rate over both tells an operator nothing.

    A subclass caught in the wrong order silently reverts this: the parent's `except`
    would match first and every never-decisioned run would be filed as not_found again.
    """
    _refuse(monkeypatch, assistant.ApplicationNeverDecisioned("never decisioned"))
    with pytest.raises(HTTPException) as raised:
        main._run_assistant(42, None, "explain")
    assert raised.value.status_code == 404
    assert raised.value.detail == "application not found"
    assert _row(rows)["refusal_code"] == "never_decisioned"


def test_the_row_carries_a_trace_id_on_the_refusal_path(rows, monkeypatch):
    """No result exists on that path, so the id comes from the entry span itself."""
    _refuse(monkeypatch, assistant.AssistantError("boom"))
    with pytest.raises(HTTPException):
        main._run_assistant(42, None, "decision")
    assert _row(rows)["trace_id"]


def test_the_policy_topic_is_recorded_and_no_free_text_is(rows, monkeypatch):
    _serve(monkeypatch)
    main._run_assistant(42, None, "explain", policy_topic="late_fees")
    row = _row(rows)
    assert row["policy_topic"] == "late_fees"
    assert "summary" not in row
    assert SERVED["summary"] not in [v for v in row.values() if isinstance(v, str)]


def test_no_exception_text_reaches_a_column(rows, monkeypatch):
    """`httpx.HTTPStatusError`'s message embeds the request URL, which embeds the app_id,
    and an `LLMError` can carry raw provider text. The entry span strips both; a column
    that accepted `str(exc)` would put them straight back."""
    _refuse(monkeypatch, LLMError("provider said: applicant SSN 123-45-6789"))
    with pytest.raises(HTTPException):
        main._run_assistant(42, None, "decision")
    written = [v for v in _row(rows).values() if isinstance(v, str)]
    assert not any("123-45-6789" in v for v in written)
    assert not any("provider said" in v for v in written)


def test_a_failed_write_never_fails_the_officer_request(monkeypatch):
    """The answer is the product; the row is a measurement of it. The realistic case is a
    volume where migration 0021 was not applied — the readiness rung in `config` is what
    reports that, because this path deliberately stays silent."""
    monkeypatch.setattr(main, "trace", _Recorder, raising=False)

    def _explode(*a, **k):
        raise RuntimeError("relation \"assistant_runs\" does not exist")

    monkeypatch.setattr(assistant_runs.db, "query", _explode)
    _serve(monkeypatch)
    assert main._run_assistant(42, None, "decision")["outcome"] == "deny"

    _refuse(monkeypatch, assistant.AssistantError("boom"))
    with pytest.raises(HTTPException) as raised:
        main._run_assistant(42, None, "decision")
    assert raised.value.status_code == 502


@pytest.mark.parametrize(
    "field,value",
    [
        ("outcome", 7),                  # not a str
        ("record_status", ""),           # empty is not an enum
        ("narration_validated", "true"), # a str is not a BOOLEAN
        ("policy_citations", True),      # isinstance(True, int) is True
    ],
)
def test_a_wrongly_typed_field_is_dropped_rather_than_written(
    rows, monkeypatch, field, value
):
    """`_charted` type-checks loosely (str OR bool for the code keys), so a bool could
    reach a TEXT column and a str a BOOLEAN one. Each column re-checks its own type here;
    a dropped field reads as "not recorded", which is true, where a coerced one would
    read as a measurement that never happened."""
    result = dict(SERVED)
    result[field] = value
    _serve(monkeypatch, result)
    main._run_assistant(42, None, "decision")
    assert _row(rows)[field] is None


# --- refusals raised in the ROUTE, above `_run_assistant` -------------------------
#
# `main._refused_before_loop` writes its own row for the three business refusals decided
# in the route bodies above the loop: the self-decision block, an overlong
# Idempotency-Key, and an unlisted policy_topic. Their codes are NOT the codes
# `_run_assistant` translates, and `record()` swallows a constraint violation by design —
# so a code the CHECK does not admit yields a span, a 4xx for the officer, and no row.

INIT_SQL = (
    Path(__file__).resolve().parents[3] / "db" / "init" / "001_schema.sql"
).read_text()
MAIN_PY = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()


def _admitted_codes() -> set[str]:
    """The refusal codes the shipped CHECK constraint actually accepts.

    Read out of the DDL rather than restated here: a hand-copied list agrees with itself
    while the column rejects the value, which is the exact failure being tested.
    """
    tail = INIT_SQL[INIT_SQL.index("CONSTRAINT ck_assistant_runs_refusal_code") :]
    listed = tail[tail.index("IN (") + 4 : tail.index("))")]
    codes = set(re.findall(r"'([a-z_]+)'", listed))
    assert codes, "could not read the refusal-code list out of the init DDL"
    return codes


@pytest.fixture
def route_refusal(monkeypatch):
    """Neutralize everything the route touches except the refusal under test.

    `assistant_runs.record` is deliberately NOT stubbed. The `rows` fixture replaces the
    database underneath the real writer instead, so what is asserted is the row the
    shipped code builds — a stubbed writer accepts every code, including one the
    constraint rejects.
    """
    monkeypatch.setattr(main.authz, "require_officer", lambda role: None)
    monkeypatch.setattr(main.authz, "deny_self_decision", lambda *a, **k: None)

    def _never(*a, **k):
        raise AssertionError("the loop ran; the refusal was supposed to precede it")

    monkeypatch.setattr(assistant, "run", _never)


def test_the_self_decision_block_is_recorded(rows, route_refusal, monkeypatch):
    def _blocked(app_id, role, uid):
        raise HTTPException(
            status_code=403,
            detail="another officer must decision this application",
        )

    monkeypatch.setattr(main.authz, "deny_self_decision", _blocked)
    with pytest.raises(HTTPException) as raised:
        main.assistant_decide(
            42, idempotency_key=None, x_user_role="underwriter",
            x_user_id="u-1", client=None,
        )
    assert raised.value.status_code == 403
    row = _row(rows)
    assert row["refusal_code"] == "self_decision"
    assert row["refusal_code"] in _admitted_codes()
    assert row["http_status"] == 403
    assert row["task"] == "decision"
    # Decided before any work starts, so a measured value would report the cost of the
    # check itself as request latency.
    assert row["latency_ms"] == 0


def test_an_overlong_idempotency_key_is_recorded(rows, route_refusal):
    with pytest.raises(HTTPException) as raised:
        main.assistant_decide(
            42, idempotency_key="k" * 65, x_user_role="underwriter",
            x_user_id="u-1", client=None,
        )
    assert raised.value.status_code == 400
    row = _row(rows)
    assert row["refusal_code"] == "idempotency_key_too_long"
    assert row["refusal_code"] in _admitted_codes()
    assert row["http_status"] == 400


def test_an_unlisted_policy_topic_is_recorded(rows, route_refusal):
    with pytest.raises(HTTPException) as raised:
        main.assistant_explain(
            42, policy_topic="not_a_real_topic", x_user_role="underwriter",
            x_user_id="u-1", client=None,
        )
    assert raised.value.status_code == 422
    row = _row(rows)
    assert row["refusal_code"] == "unknown_policy_topic"
    assert row["refusal_code"] in _admitted_codes()
    assert row["task"] == "explain"
    # The rejected value is the officer's own free text and reaches no column.
    assert "not_a_real_topic" not in [v for v in row.values() if isinstance(v, str)]


def test_no_route_refusal_writes_a_code_the_constraint_rejects():
    """The class, not the three instances above.

    A refusal added to a route later — with no test of its own — still has to be a code
    the column admits, or its row is discarded by a write that never raises.
    """
    codes = set(re.findall(r'code="([a-z_]+)"', MAIN_PY))
    assert len(codes) >= 3, (
        "the _refused_before_loop call sites changed shape; this scan now reads nothing"
    )
    assert codes <= _admitted_codes(), (
        f"refused above the loop but not admitted by the CHECK: "
        f"{sorted(codes - _admitted_codes())}"
    )
