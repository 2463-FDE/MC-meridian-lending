"""Decisioning-assistant agent tests (ADR 0009 §5, spec D4).

All offline: the model is FakeAdapter with scripted responses; the tools
(decision-service HTTP) are monkeypatched. Covers the loop, the record-validation
gate (recorded facts beat narration), fail-closed paths, and the redaction
compatibility of history turns.
"""

import io
import json
import logging
from contextlib import contextmanager

import httpx
import pytest

from tests.test_native_script import native_adapter

from app import assistant
from app.llm import ClaudeClient, FakeAdapter, LLMConfig
from app.llm.request_builder import redact_json


@contextmanager
def _capture_assistant_log():
    """`get_logger("assistant")` sets `propagate = False` (own its handlers, keep
    LLM/PII content out of uvicorn/root), so `caplog` -- which hooks the root
    logger -- never sees its records. Attach a handler directly, same as
    test_llm_client.py's `test_key_not_logged_on_call_or_error`."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    log = logging.getLogger("assistant")
    log.addHandler(handler)
    try:
        yield buf
    finally:
        log.removeHandler(handler)


@pytest.fixture(autouse=True)
def _kyc_passes(monkeypatch):
    # The assistant tests exercise the agent loop / idempotency forwarding, not the ADR
    # 0011 KYC gate on the score tool (covered in test_kyc_gate.py). Let KYC pass so its
    # DB lookup / 409 doesn't interfere.
    monkeypatch.setattr(assistant.kyc_gate, "require_kyc_passed", lambda app_id: None)


def _client(*responses):
    cfg = LLMConfig(
        api_key="test-key", max_retries=0, token_budget=20_000, max_tokens=256
    )
    adapter = native_adapter(*responses)
    return ClaudeClient(cfg, adapter=adapter), adapter


TOOL_CALL = json.dumps(
    {"action": "tool", "tool": "score_application", "input": {"application_id": 42}}
)
FINAL_DENY = json.dumps(
    {
        "action": "final",
        "outcome": "deny",
        "reason_codes": ["R02", "R03"],
        "summary": "The application was denied: obligations are excessive relative "
        "to income, and income is insufficient for the amount requested.",
    }
)

SCORE_RESULT = {
    "status": "recorded",
    "outcome": "deny",
    "score": 518,
    "policy_band": "deny",
    "reason_codes": ["R02", "R03"],
}
RECORD_BODY = {
    "application_id": 42,
    "status": "recorded",
    "outcome": "deny",
    "policy_band": "deny",
    "principal_reasons": [
        {
            "code": "R02",
            "reason": "Excessive obligations in relation to income",
            "feature": "payment_burden",
        },
        {
            "code": "R03",
            "reason": "Income insufficient for amount of credit requested",
            "feature": "income_sufficiency",
        },
    ],
    "drivers": {"model_score": 518},
    "inputs": {"bureau_score": 612},
    "decided_by": "meridian-risk-stub:v1",
    "decided_at": "2026-07-15T12:00:00",
}


class _FakeRecordResponse:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return RECORD_BODY


class _NotFoundResponse:
    status_code = 404


@pytest.fixture
def tools(monkeypatch):
    """Stub both tools' HTTP seams; record score-tool invocations."""
    calls = {"score": 0}

    def _score(app_id, request_id=None):
        calls["score"] += 1
        return dict(SCORE_RESULT)

    monkeypatch.setitem(assistant._TOOLS, "score_application", _score)
    monkeypatch.setattr(
        assistant.clients, "get", lambda base, path: _FakeRecordResponse()
    )
    return calls


def test_happy_path_tool_then_validated_final(tools):
    client, adapter = _client(TOOL_CALL, FINAL_DENY)
    result = assistant.run(42, client)
    assert tools["score"] == 1
    assert result["outcome"] == "deny"
    assert [r["code"] for r in result["principal_reasons"]] == ["R02", "R03"]
    assert result["narration_validated"] is True
    # Summary is record-derived, never the model's prose: shows the recorded outcome.
    assert "deny" in result["summary"] and "R02" in result["summary"]
    assert result["decided_by"] == "meridian-risk-stub:v1"


def test_response_carries_the_recorded_model_score(tools):
    """The officer screen must show the SAME decision facts for an assistant run as for a
    manual Run decision (score + adverse-action reason), so the response carries the
    record's model score. Without it the primary decision panel is blank after an
    assistant-recorded outcome while the assistant card shows one (PR #11 review)."""
    client, _ = _client(TOOL_CALL, FINAL_DENY)
    result = assistant.run(42, client)
    assert result["score"] == RECORD_BODY["drivers"]["model_score"]
    # Record-derived, not narration-derived: the model never supplies the score.
    assert result["score"] == 518


def test_contradicting_narration_is_replaced_by_recorded_facts(tools):
    lying_final = json.dumps(
        {
            "action": "final",
            "outcome": "approve",
            "reason_codes": [],
            "summary": "Approved with no concerns.",
        }
    )
    client, _ = _client(TOOL_CALL, lying_final)
    result = assistant.run(42, client)
    # The record wins: outcome/reasons come from the persisted event, narration dropped.
    assert result["outcome"] == "deny"
    assert result["narration_validated"] is False
    assert "Approved with no concerns" not in result["summary"]
    assert "R02" in result["summary"]


def test_final_without_recorded_decision_is_refused(monkeypatch):
    monkeypatch.setattr(
        assistant.clients, "get", lambda base, path: _NotFoundResponse()
    )
    client, _ = _client(
        json.dumps(
            {
                "action": "final",
                "outcome": "approve",
                "reason_codes": [],
                "summary": "ok",
            }
        )
    )
    with pytest.raises(assistant.AssistantError, match="no decision record"):
        assistant.run(42, client)


def test_unknown_tool_is_refused(tools, monkeypatch):
    # Schema-legal tool name with no registered implementation must refuse, not 500.
    client, _ = _client(
        json.dumps(
            {
                "action": "tool",
                "tool": "get_decision_record",
                "input": {"application_id": 42},
            }
        )
    )
    monkeypatch.delitem(assistant._TOOLS, "get_decision_record")
    with pytest.raises(assistant.AssistantError, match="unknown tool"):
        assistant.run(42, client)


def test_step_budget_exhaustion_is_refused(tools):
    client, _ = _client(*([TOOL_CALL] * assistant._MAX_STEPS))
    with pytest.raises(assistant.AssistantError, match="no final answer"):
        assistant.run(42, client)


def test_step_budget_exhaustion_after_scoring_marks_scored(tools):
    """The soft-stop refusal (_terminal_action, interlock 4's soft half): the model
    called score_application (a decision_events row is durably written) before
    exhausting its step budget on the narrate turn. The refusal must not read as
    though nothing happened -- the exception carries `.scored` for main.py's refusal
    handler, and the log line says it too, so an operator can tell "recorded, but the
    run refused" from "nothing happened" without correlating decision-service's own
    event log."""
    client, _ = _client(*([TOOL_CALL] * assistant._MAX_STEPS))
    with _capture_assistant_log() as log_buf:
        with pytest.raises(assistant.AssistantError, match="no final answer") as exc:
            assistant.run(42, client)
    assert tools["score"] == 1  # the decision was recorded before the refusal
    assert exc.value.scored is True
    assert "scored=True" in log_buf.getvalue()


def test_step_budget_exhaustion_before_scoring_marks_unscored(monkeypatch):
    """The same refusal on a run that never reached score_application must not claim
    a decision was recorded when none was."""
    monkeypatch.setattr(
        assistant.clients, "get", lambda base, path: _FakeRecordResponse()
    )
    non_scoring_call = json.dumps(
        {
            "action": "tool",
            "tool": "get_decision_record",
            "input": {"application_id": 42},
        }
    )
    client, _ = _client(*([non_scoring_call] * assistant._MAX_STEPS))
    with _capture_assistant_log() as log_buf:
        with pytest.raises(assistant.AssistantError, match="no final answer") as exc:
            assistant.run(42, client)
    assert exc.value.scored is False
    assert "scored=False" in log_buf.getvalue()


def test_recursion_error_after_scoring_marks_scored(tools, monkeypatch):
    """The hard half of interlock 4 (`GraphRecursionError`, not currently reachable
    on the pinned langgraph -- see test_agentic_loop.py's interlock 4 tests -- but the
    except block is real code and must carry the same signal as the soft half."""

    class _RaisingAgent:
        def __init__(self, tools):
            self._score_tool = tools[0]  # score_application, built first

        def invoke(self, inputs, config=None):
            self._score_tool.func()
            raise assistant.GraphRecursionError("recursion limit reached")

    monkeypatch.setattr(
        assistant, "_build_agent", lambda client, tools: _RaisingAgent(tools)
    )
    client, _ = _client(TOOL_CALL)
    with _capture_assistant_log() as log_buf:
        with pytest.raises(assistant.AssistantError, match="no final answer") as exc:
            assistant.run(42, client)
    assert tools["score"] == 1
    assert exc.value.scored is True
    assert "scored=True" in log_buf.getvalue()


def test_tool_uses_officer_app_id_not_model_echo(tools, monkeypatch):
    seen = []

    def _score(app_id, request_id=None):
        seen.append(app_id)
        return dict(SCORE_RESULT)

    assistant._TOOLS["score_application"] = _score
    wandering = json.dumps(
        {
            "action": "tool",
            "tool": "score_application",
            "input": {"application_id": 999},
        }
    )
    client, _ = _client(wandering, FINAL_DENY)
    assistant.run(42, client)
    assert seen == [42]  # the model cannot wander to another applicant's file


def _turn_text(content) -> str:
    """Every string a built turn actually carries, whatever shape it carries it in."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        body = block.get("content")
        parts.append(body if isinstance(body, str) else json.dumps(block.get("input")))
    return " ".join(parts)


def test_history_turns_survive_redaction_intact(tools):
    client, adapter = _client(TOOL_CALL, FINAL_DENY)
    assistant.run(42, client)
    # Second model call carries the tool round-trip as history; the enum vocabulary
    # must pass the fail-closed redactor unmasked or the agent would go blind.
    final_req = adapter.calls[-1]
    # Shape-agnostic: a turn's content is a JSON string on the JSON-action path and a
    # list of tool_use / tool_result blocks under native tool calling. Read the payload
    # out of either, rather than assuming the one that happens to be live.
    joined = " ".join(_turn_text(m["content"]) for m in final_req.messages[:-1])
    assert '"deny"' in joined and '"R02"' in joined and "518" in joined
    assert "•" not in joined  # nothing in the tool round-trip was masked


def test_tool_result_json_passes_redactor_verbatim():
    payload = json.dumps({"tool": "score_application", "result": SCORE_RESULT})
    assert json.loads(redact_json(payload)) == json.loads(payload)


def test_endpoint_returns_503_when_llm_disabled(monkeypatch):
    monkeypatch.delenv("LLM_ENABLED", raising=False)
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as tc:
        assert tc.post("/assistant/decisions/42").status_code == 503
        assert tc.get("/assistant/decisions/42").status_code == 503


def test_endpoint_forwards_idempotency_key_header_as_request_id(monkeypatch):
    # PR #7 review: the assistant decision endpoint must honour the standard
    # Idempotency-Key header (not only a query param), so a retry with the same header
    # forwards the same request_id to decision-service instead of a fresh UUID that
    # would re-pull credit and append a second regulated event.
    from fastapi.testclient import TestClient

    from app import main
    from app.main import app

    forwarded = []

    def _post(base, path, payload):
        forwarded.append(payload.get("request_id"))
        return {
            "outcome": "deny",
            "score": 518,
            "policy_band": "deny",
            "principal_reasons": [
                {"code": "R02", "reason": "x", "feature": "payment_burden"},
                {"code": "R03", "reason": "y", "feature": "income_sufficiency"},
            ],
        }

    monkeypatch.setattr(assistant.clients, "post", _post)
    monkeypatch.setattr(
        assistant.clients, "get", lambda base, path: _FakeRecordResponse()
    )
    monkeypatch.setattr(
        assistant, "decision_request_payload", lambda app_id: {"application_id": app_id}
    )
    # A fresh scripted client per request (each run consumes tool-call + final).
    app.dependency_overrides[main.get_llm_client] = lambda: _client(
        TOOL_CALL, FINAL_DENY
    )[0]
    try:
        tc = TestClient(app)
        # X-User-Role: the assistant is officer-only (PR review); the gateway forwards
        # the session role and origination requires it.
        headers = {"Idempotency-Key": "officer-key-1", "X-User-Role": "underwriter"}
        assert tc.post("/assistant/decisions/42", headers=headers).status_code == 200
        assert tc.post("/assistant/decisions/42", headers=headers).status_code == 200
    finally:
        app.dependency_overrides.clear()

    # Both retries forwarded the SAME caller-supplied key — not two fresh UUIDs.
    assert forwarded == ["officer-key-1", "officer-key-1"]


def test_endpoint_rejects_overlong_idempotency_key(monkeypatch):
    # Same 64-char limit as /applications/{app_id}/decision: a clean 400, not a
    # confusing downstream 503.
    from fastapi.testclient import TestClient

    from app import main
    from app.main import app

    app.dependency_overrides[main.get_llm_client] = lambda: _client(FINAL_DENY)[0]
    try:
        tc = TestClient(app)
        resp = tc.post(
            "/assistant/decisions/42",
            headers={"Idempotency-Key": "x" * 65, "X-User-Role": "underwriter"},
        )
        assert resp.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_assistant_requires_officer_role(monkeypatch):
    # PR review: the assistant is officer-only and must not be triggerable anonymously
    # through the /los proxy. No X-User-Role (or a non-officer role) -> 403, before any
    # scoring. LLM is enabled via the override so the 403 is the role gate, not the
    # LLM-disabled 503.
    from fastapi.testclient import TestClient

    from app import main
    from app.main import app

    app.dependency_overrides[main.get_llm_client] = lambda: _client(FINAL_DENY)[0]
    try:
        tc = TestClient(app, raise_server_exceptions=False)
        assert tc.post("/assistant/decisions/42").status_code == 403
        assert tc.get("/assistant/decisions/42").status_code == 403
        assert (
            tc.post(
                "/assistant/decisions/42", headers={"X-User-Role": "borrower"}
            ).status_code
            == 403
        )
    finally:
        app.dependency_overrides.clear()


def test_assistant_route_surfaces_the_rate_limit(monkeypatch):
    # Wiring only: the limiter's own counting/window logic is unit-tested in
    # test_rate_limit.py. This proves both routes actually call it, after the
    # officer-role gate, and surface its 429 rather than swallowing it.
    from fastapi import HTTPException
    from fastapi.testclient import TestClient

    from app import main
    from app.main import app

    def _tripped(user_id):
        raise HTTPException(status_code=429, detail="assistant rate limit exceeded")

    monkeypatch.setattr(main.rate_limit, "check_llm_rate_limit", _tripped)
    app.dependency_overrides[main.get_llm_client] = lambda: _client(FINAL_DENY)[0]
    try:
        tc = TestClient(app, raise_server_exceptions=False)
        assert (
            tc.post(
                "/assistant/decisions/42", headers={"X-User-Role": "underwriter"}
            ).status_code
            == 429
        )
        assert (
            tc.get(
                "/assistant/decisions/42", headers={"X-User-Role": "underwriter"}
            ).status_code
            == 429
        )
    finally:
        app.dependency_overrides.clear()


# --- Adversarial-review fixes (teeth 2026-07-15) ------------------------------------


def test_repeated_score_requests_execute_once(tools):
    # H2: the model cannot compound bureau pulls / decision events in one request —
    # repeat score requests are served from the run-local cache.
    client, _ = _client(TOOL_CALL, TOOL_CALL, TOOL_CALL, FINAL_DENY)
    result = assistant.run(42, client)
    assert tools["score"] == 1
    assert result["outcome"] == "deny"


def test_explain_task_never_scores(tools, monkeypatch):
    # M4: read-only explain — even a model that asks to score gets the record instead.
    record_result = {
        "status": "recorded",
        "outcome": "deny",
        "policy_band": "deny",
        "score": 518,
        "reason_codes": ["R02", "R03"],
    }
    monkeypatch.setitem(
        assistant._TOOLS, "get_decision_record", lambda app_id: dict(record_result)
    )
    client, _ = _client(TOOL_CALL, FINAL_DENY)  # model (wrongly) asks to score
    result = assistant.run(42, client, task="explain")
    assert tools["score"] == 0  # no fresh credit pull, ever, on explain
    assert result["outcome"] == "deny"
    assert result["record_status"] == "recorded"


def test_explain_legacy_record_answers_honestly(monkeypatch):
    # M4/ADR 0008 req.4: legacy outcome (e.g. #6012) — reasons unrecoverable, say so.
    legacy_body = {
        "application_id": 6012,
        "status": "no_record_legacy",
        "outcome": "deny",
        "policy_band": None,
        "principal_reasons": [],
        "drivers": {},
        "inputs": {},
        "decided_by": None,
        "decided_at": None,
    }

    class _LegacyResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return legacy_body

    monkeypatch.setattr(assistant.clients, "get", lambda base, path: _LegacyResp())
    monkeypatch.setitem(
        assistant._TOOLS,
        "get_decision_record",
        lambda app_id: {
            "status": "no_record_legacy",
            "outcome": "deny",
            "policy_band": None,
            "score": None,
            "reason_codes": [],
        },
    )
    record_call = json.dumps(
        {
            "action": "tool",
            "tool": "get_decision_record",
            "input": {"application_id": 6012},
        }
    )
    final = json.dumps(
        {
            "action": "final",
            "outcome": "deny",
            "reason_codes": [],
            "summary": "ignored — legacy summaries are constructed, never narrated",
        }
    )
    client, _ = _client(record_call, final)
    result = assistant.run(6012, client, task="explain")
    assert result["record_status"] == "no_record_legacy"
    assert result["outcome"] == "deny"
    assert result["principal_reasons"] == []
    assert "never recorded" in result["summary"]
    # Legacy events never captured drivers: report the score as absent, never invent one.
    assert result["score"] is None


def test_explain_never_decisioned_raises_not_found(monkeypatch):
    monkeypatch.setattr(
        assistant.clients, "get", lambda base, path: _NotFoundResponse()
    )
    monkeypatch.setitem(
        assistant._TOOLS, "get_decision_record", lambda app_id: {"status": "not_found"}
    )
    record_call = json.dumps(
        {
            "action": "tool",
            "tool": "get_decision_record",
            "input": {"application_id": 7},
        }
    )
    final = json.dumps({"action": "final", "summary": "nothing found"})
    client, _ = _client(record_call, final)
    with pytest.raises(assistant.ApplicationNotFound):
        assistant.run(7, client, task="explain")


def _error_response(app_id, request_id=None):
    """A real httpx.Response so raise_for_status() produces httpx's own message --
    which embeds the request URL -- the same way it would against a live
    decision-service."""
    path = f"/decisions/{app_id}/record"
    if request_id:
        path += f"?request_id={request_id}"
    return httpx.Response(
        500, request=httpx.Request("GET", f"http://decision-service{path}")
    )


def test_score_application_not_found_message_is_identifier_free(monkeypatch):
    # B1 follow-up: this raises through the tool/step/root trace spans, and
    # langsmith's trace() attaches str(exception) to the span's `error` field on
    # exit -- so app_id in the message would leak to LangSmith the same way the
    # root metadata dict did before B1's fix.
    monkeypatch.setattr(assistant, "decision_request_payload", lambda app_id: None)
    with pytest.raises(assistant.ApplicationNotFound) as exc_info:
        assistant._score_application(918273)
    assert "918273" not in str(exc_info.value)


def test_explain_never_decisioned_message_is_identifier_free(monkeypatch):
    monkeypatch.setattr(
        assistant.clients, "get", lambda base, path: _NotFoundResponse()
    )
    with pytest.raises(assistant.ApplicationNotFound) as exc_info:
        assistant._validated_final({}, 918273, "explain")
    assert "918273" not in str(exc_info.value)


def test_get_decision_record_error_scrubs_identifier_and_chain(monkeypatch):
    # httpx.HTTPStatusError's own message embeds the request URL
    # (/decisions/918273/record), so the wrapping AssistantError must not chain
    # to it either -- `from exc` (or no `from` at all) would leave the original
    # exception on __context__/__cause__, and traceback.format_exception (which
    # langsmith's trace() calls on error) prints the whole chain.
    monkeypatch.setattr(
        assistant.clients, "get", lambda base, path: _error_response(918273)
    )
    with pytest.raises(assistant.AssistantError) as exc_info:
        assistant._get_decision_record(918273)
    exc = exc_info.value
    assert "918273" not in str(exc)
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True


def test_validated_final_record_fetch_error_scrubs_identifier_and_chain(monkeypatch):
    monkeypatch.setattr(
        assistant.clients,
        "get",
        lambda base, path: _error_response(918273, request_id="req-abc123"),
    )
    with pytest.raises(assistant.AssistantError) as exc_info:
        assistant._validated_final({}, 918273, "decision", "req-abc123")
    exc = exc_info.value
    assert "918273" not in str(exc)
    assert "req-abc123" not in str(exc)
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True


def test_request_id_forwarded_to_decision_service(monkeypatch):
    # PR #7 review: the officer request's idempotency key must reach decision-service
    # so a retried request replays the recorded decision instead of re-decisioning.
    captured = {}
    monkeypatch.setattr(
        assistant, "decision_request_payload", lambda app_id: {"application_id": app_id}
    )

    def _post(base, path, payload):
        captured.update(payload)
        return {
            "outcome": "deny",
            "score": 518,
            "policy_band": "deny",
            "principal_reasons": [
                {"code": "R02", "reason": "x", "feature": "payment_burden"}
            ],
        }

    monkeypatch.setattr(assistant.clients, "post", _post)
    result = assistant._score_application(42, "officer-req-1")
    assert captured["request_id"] == "officer-req-1"
    assert result["outcome"] == "deny"
    # And without a key, none is sent (explicit re-decision path).
    captured.clear()
    assistant._score_application(42)
    assert "request_id" not in captured


def test_final_validated_against_request_scoped_event_not_app_latest(monkeypatch):
    # PR #7 review: a concurrent re-decision landing between scoring and final
    # validation must not swap the validated record. The record fetch is scoped to this
    # run's request_id, so validation binds to the event this request created even when
    # a NEWER (different) event exists for the same application.
    newer_body = {
        **RECORD_BODY,
        "outcome": "approve",
        "policy_band": "approve",
        "principal_reasons": [],
    }

    def _get(base, path):
        if "request_id=" in path:
            return _FakeRecordResponse()  # this request's event: deny / R02,R03

        class _NewerResponse(_FakeRecordResponse):
            def json(self):
                return newer_body  # what an unscoped app-latest fetch would return

        return _NewerResponse()

    monkeypatch.setattr(assistant.clients, "get", _get)

    def _score(app_id, request_id=None):
        # request_id is always present now (run() auto-generates one), and it is what
        # scopes the validation fetch below.
        assert request_id
        return dict(SCORE_RESULT)

    monkeypatch.setitem(assistant._TOOLS, "score_application", _score)
    client, _ = _client(TOOL_CALL, FINAL_DENY)
    result = assistant.run(42, client)
    # Bound to our own event, not the concurrent 'approve' that landed after scoring.
    assert result["outcome"] == "deny"
    assert result["narration_validated"] is True
    assert [r["code"] for r in result["principal_reasons"]] == ["R02", "R03"]


def test_structurally_valid_final_with_lying_summary_is_not_passed_through(tools):
    # PR #7 review: a model can clear the structured outcome/reason_codes check yet
    # narrate a contradictory summary. The officer summary is always record-derived, so
    # the lie never reaches the officer even though narration_validated is True.
    lying_but_valid = json.dumps(
        {
            "action": "final",
            "outcome": "deny",  # matches the record
            "reason_codes": ["R02", "R03"],  # matches the record
            "summary": "Great news — this loan was APPROVED and funds are on the way.",
        }
    )
    client, _ = _client(TOOL_CALL, lying_but_valid)
    result = assistant.run(42, client)
    assert result["outcome"] == "deny"
    assert result["narration_validated"] is True  # structured claim did match
    assert "approved" not in result["summary"].lower()
    assert "deny" in result["summary"] and "R02" in result["summary"]


def test_assistant_route_422s_on_persisted_null_debt(monkeypatch):
    # PR #7 review regression: the assistant score tool builds the same decision payload,
    # so a persisted NULL monthly_debt must quarantine here too — surfacing as 422, not a
    # zero-debt decision (and not a 500 from the global handler).
    from fastapi.testclient import TestClient

    from app import main
    from app.main import app as fastapi_app
    from app.routers import applications as apps_router

    null_row = {
        "applicant_id": 9,
        "amount": 15000,
        "term_months": 36,
        "income": 50000,
        "monthly_debt": None,
        "employment_years": 3,
        "name": "Legacy",
        "ssn": "123456789",
    }
    monkeypatch.setattr(apps_router.db, "query", lambda sql, params=None: [null_row])
    fastapi_app.dependency_overrides[main.get_llm_client] = lambda: _client(
        TOOL_CALL, FINAL_DENY
    )[0]
    try:
        resp = TestClient(fastapi_app, raise_server_exceptions=False).post(
            "/assistant/decisions/1", headers={"X-User-Role": "underwriter"}
        )
        assert resp.status_code == 422
    finally:
        fastapi_app.dependency_overrides.clear()


def test_empty_summary_falls_back_to_record_summary(tools):
    # L1: matching facts but no narration — officer still gets a summary, from the record.
    final_no_summary = json.dumps(
        {"action": "final", "outcome": "deny", "reason_codes": ["R02", "R03"]}
    )
    client, _ = _client(TOOL_CALL, final_no_summary)
    result = assistant.run(42, client)
    assert result["narration_validated"] is True
    assert "R02" in result["summary"] and "deny" in result["summary"]


# --- Staff self-decision block: the assistant is the second decisioning path ----------
#
# The client scoped their ask to "the one route that runs a decision", having seen only
# POST /applications/{id}/decision. The assistant's score tool performs the same
# regulated decision and appends the same decision_events record (assistant.py score
# tool -> decision-service /decisions), so blocking only the manual route leaves an
# underwriter able to decision their own application through the assistant panel.
# Both entry points carry the block. GET /assistant/decisions (explain) is read-only and
# never scores, so it stays open -- "leave every other officer action alone".


def test_assistant_cannot_decision_the_callers_own_application(monkeypatch):
    from fastapi.testclient import TestClient

    from app import authz, main
    from app.main import app

    def _q(sql, params=None):
        if "FROM users" in sql:
            return [{"applicant_id": 4}]
        if "FROM applications" in sql:
            return [{"applicant_id": 4, "submitted_by_user_id": None}]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(authz.db, "query", _q)

    def _never(*a, **k):
        raise AssertionError("a blocked self-decision must not run the assistant")

    monkeypatch.setattr(main, "_run_assistant", _never)
    app.dependency_overrides[main.get_llm_client] = lambda: _client(FINAL_DENY)[0]
    try:
        tc = TestClient(app, raise_server_exceptions=False)
        blocked = tc.post(
            "/assistant/decisions/42",
            headers={"X-User-Role": "underwriter", "X-User-Id": "9"},
        )
        assert blocked.status_code == 403
        # Read-only explain is untouched: it never scores.
        assert (
            tc.get(
                "/assistant/decisions/42",
                headers={"X-User-Role": "underwriter", "X-User-Id": "9"},
            ).status_code
            != 403
        )
    finally:
        app.dependency_overrides.clear()


def test_assistant_cannot_decision_own_self_submitted_unlinked_application(monkeypatch):
    # D24 (docs/debt-log.md), PR #38 review: account linkage alone cannot catch an officer
    # who submitted their own application through the ordinary apply flow -- intake never
    # links the fresh applicants row to users.applicant_id. Same shape as the test above,
    # through the assistant path, but with no account linkage (caller_applicant_id=None,
    # the usual staff shape per PR #38) and submitted_by_user_id matching the caller instead.
    from fastapi.testclient import TestClient

    from app import authz, main
    from app.main import app

    def _q(sql, params=None):
        if "FROM users" in sql:
            return [{"applicant_id": None}]
        if "FROM applications" in sql:
            return [{"applicant_id": 42, "submitted_by_user_id": 9}]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(authz.db, "query", _q)

    def _never(*a, **k):
        raise AssertionError("a blocked self-decision must not run the assistant")

    monkeypatch.setattr(main, "_run_assistant", _never)
    app.dependency_overrides[main.get_llm_client] = lambda: _client(FINAL_DENY)[0]
    try:
        tc = TestClient(app, raise_server_exceptions=False)
        blocked = tc.post(
            "/assistant/decisions/42",
            headers={"X-User-Role": "underwriter", "X-User-Id": "9"},
        )
        assert blocked.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_assistant_legacy_null_submitter_self_decision_is_not_blocked(monkeypatch):
    # D24 residual (docs/debt-log.md; PR #38 review, round 3): a pre-migration-0017 row
    # (or any genuinely anonymous submit) has submitted_by_user_id NULL, identical at the
    # SQL level to a real anonymous applicant -- no code-level check can tell them apart.
    # Route-level pin through the assistant path, matching the run_decision route pin in
    # test_decision_route.py. Mitigated operationally (docs/runbooks/operations.md), not in code.
    from fastapi.testclient import TestClient

    from app import authz, main
    from app.main import app

    def _q(sql, params=None):
        if "FROM users" in sql:
            return [{"applicant_id": None}]
        if "FROM applications" in sql:
            return [{"applicant_id": 42, "submitted_by_user_id": None}]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(authz.db, "query", _q)
    monkeypatch.setattr(
        main, "_run_assistant", lambda *a, **k: {"decision": "not-blocked-marker"}
    )
    app.dependency_overrides[main.get_llm_client] = lambda: _client(FINAL_DENY)[0]
    try:
        tc = TestClient(app, raise_server_exceptions=False)
        resp = tc.post(
            "/assistant/decisions/42",
            headers={"X-User-Role": "underwriter", "X-User-Id": "9"},
        )
        assert resp.status_code == 200  # not blocked -- the documented residual
        assert resp.json() == {"decision": "not-blocked-marker"}
    finally:
        app.dependency_overrides.clear()
