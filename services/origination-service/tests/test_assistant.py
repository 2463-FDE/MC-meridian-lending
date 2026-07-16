"""Decisioning-assistant agent tests (ADR 0009 §5, spec D4).

All offline: the model is FakeAdapter with scripted responses; the tools
(decision-service HTTP) are monkeypatched. Covers the loop, the record-validation
gate (recorded facts beat narration), fail-closed paths, and the redaction
compatibility of history turns.
"""

import json

import pytest

from app import assistant
from app.llm import ClaudeClient, FakeAdapter, LLMConfig
from app.llm.request_builder import redact_json


def _client(*responses):
    cfg = LLMConfig(
        api_key="test-key", max_retries=0, token_budget=20_000, max_tokens=256
    )
    adapter = FakeAdapter(responses=list(responses))
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


def test_history_turns_survive_redaction_intact(tools):
    client, adapter = _client(TOOL_CALL, FINAL_DENY)
    assistant.run(42, client)
    # Second model call carries the tool round-trip as history; the enum vocabulary
    # must pass the fail-closed redactor unmasked or the agent would go blind.
    final_req = adapter.calls[-1]
    history_contents = [m["content"] for m in final_req.messages[:-1]]
    joined = " ".join(history_contents)
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
        headers = {"Idempotency-Key": "officer-key-1"}
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
        resp = tc.post("/assistant/decisions/42", headers={"Idempotency-Key": "x" * 65})
        assert resp.status_code == 400
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
            "/assistant/decisions/1"
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
