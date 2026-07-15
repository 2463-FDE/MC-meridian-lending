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

    def _score(app_id):
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
    assert "denied" in result["summary"]
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

    def _score(app_id):
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
        resp = tc.post("/assistant/decisions/42")
    assert resp.status_code == 503
