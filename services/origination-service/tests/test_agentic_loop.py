"""Slice 4b: the framework owns the officer loop, and the four interlocks survive it.

Every assertion here is against the OUTBOUND `CompletionRequest` (`FakeAdapter.calls`)
or against a real dispatch count — never against the seam's intent. Slice 2 shipped a
query strip that never matched (a leading-underscore class attribute on a pydantic
model becomes a `ModelPrivateAttr`, so the comparison compared against the wrong
object) and it was caught only because a test read the request that would have gone to
the provider. Same rule applies to every test in this file.

Two of these are red until the swap lands (`test_the_framework_drives_the_loop`,
`test_framework_span_content_is_hidden_in_code`). The other four hold today against the
hand-rolled loop AND must hold after it: an interlock test that only passes on one side
of the swap proves nothing about the swap.
"""

import json

import pytest

from app import assistant
from app.llm import ClaudeClient, FakeAdapter, LLMConfig

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(autouse=True)
def _kyc_passes(monkeypatch):
    monkeypatch.setattr(assistant.kyc_gate, "require_kyc_passed", lambda app_id: None)


def _client(*responses):
    cfg = LLMConfig(
        api_key="test-key", max_retries=0, token_budget=40_000, max_tokens=256
    )
    adapter = FakeAdapter(responses=list(responses))
    return ClaudeClient(cfg, adapter=adapter), adapter


SCORE = json.dumps(
    {"action": "tool", "tool": "score_application", "input": {"application_id": 42}}
)
# The model asking about ANOTHER applicant's file. The officer's own request is 42.
SCORE_ELSEWHERE = json.dumps(
    {"action": "tool", "tool": "score_application", "input": {"application_id": 99}}
)
SEARCH = json.dumps(
    {
        "action": "tool",
        "tool": "search_policy",
        "input": {"query": "what is the DTI cap for a 640 bureau score"},
    }
)
FINAL = json.dumps(
    {
        "action": "final",
        "outcome": "deny",
        "reason_codes": ["R02", "R03"],
        "summary": "Denied: obligations excessive relative to income.",
    }
)

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
SCORE_RESULT = {
    "status": "recorded",
    "outcome": "deny",
    "score": 518,
    "policy_band": "deny",
    "reason_codes": ["R02", "R03"],
}


class _RecordResponse:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return RECORD_BODY


@pytest.fixture
def dispatched(monkeypatch):
    """Record every tool dispatch, with the id it was actually called with.

    Patches the entries in `assistant._TOOLS` rather than the module functions, so a
    swap that snapshots the tool table at IMPORT time instead of per request fails
    here — the framework must build its tool set from `_TOOLS` when `run()` is called.
    """
    seen: list[tuple[str, int]] = []

    def _score(app_id, request_id=None):
        seen.append(("score_application", app_id))
        return dict(SCORE_RESULT)

    def _record(app_id):
        seen.append(("get_decision_record", app_id))
        return {
            "status": "recorded",
            "outcome": "deny",
            "policy_band": "deny",
            "reason_codes": ["R02", "R03"],
        }

    monkeypatch.setitem(assistant._TOOLS, "score_application", _score)
    monkeypatch.setitem(assistant._TOOLS, "get_decision_record", _record)
    monkeypatch.setattr(assistant.clients, "get", lambda base, path: _RecordResponse())
    return seen


def _outbound(adapter) -> str:
    """Every outbound request, serialized. What would have reached the provider."""
    assert adapter.calls, "nothing reached the adapter — assert against a real request"
    return json.dumps(
        [
            {"system": c.system, "messages": c.messages, "tools": c.tools}
            for c in adapter.calls
        ],
        default=str,
    )


# --- the swap itself ----------------------------------------------------------------


def test_the_framework_drives_the_loop(dispatched):
    """RED until slice 4b. The freeze fail condition is a framework in the repo but not
    in the request path, so this asserts the request path itself: `run()` builds a
    langgraph graph and that graph executes the turns."""
    graph = assistant._build_agent  # AttributeError until the swap lands
    seen = {}

    def _spy(*args, **kwargs):
        built = graph(*args, **kwargs)
        seen["type"] = type(built).__module__
        return built

    client, _ = _client(SCORE, FINAL)
    assistant._build_agent = _spy
    try:
        assistant.run(42, client)
    finally:
        assistant._build_agent = graph
    assert seen.get("type", "").startswith("langgraph"), (
        f"the loop ran on {seen.get('type')!r}, not a langgraph graph"
    )


def test_framework_span_content_is_hidden_in_code(dispatched):
    """RED until slice 4b. A framework tracer ships the graph state it is handed, and
    that state carries the model's prose, its narration, and the model-authored policy
    query — all three forbidden by the CONTENT RULE at assistant.py:44. Measured: with
    an unprimed client, 'DTI cap', 'Maria' and the narration all reach the span.

    The control must be code-owned, not a shell variable: `LLM_TRACE_CONTENT` already
    shows that a compose gate cannot stop an operator's shell. Priming the LangSmith
    singleton at startup means the process cannot post run inputs/outputs at all, while
    `_hide_run_metadata` leaves our explicit spans (whose entire signal is metadata)
    intact."""
    from langsmith.run_trees import get_cached_client

    from app.llm.config import harden_trace_client

    harden_trace_client()
    client_ls = get_cached_client()
    assert client_ls._hide_inputs is True, (
        "the LangSmith client was not primed with hide_inputs — framework spans would "
        "carry graph state (model prose, narration, the policy query)"
    )
    assert client_ls._hide_outputs is True
    assert client_ls._hide_metadata is False, (
        "hiding metadata would blank our own CONTENT-RULE spans, which carry all their "
        "signal there"
    )


# --- interlock 1: the regulated decision happens at most once ------------------------


def test_interlock_1_two_score_requests_produce_one_regulated_decision(dispatched):
    client, adapter = _client(SCORE, SCORE, FINAL)
    assistant.run(42, client)
    scores = [name for name, _ in dispatched if name == "score_application"]
    assert len(scores) == 1, (
        f"{len(scores)} regulated decisions in one turn: {dispatched}"
    )


# --- interlock 2: explain never buys a credit pull -----------------------------------


def test_interlock_2_explain_never_scores(dispatched):
    client, adapter = _client(SCORE, FINAL)
    assistant.run(42, client, task="explain")
    assert not [n for n, _ in dispatched if n == "score_application"], (
        f"a read-only explain task bought a credit pull: {dispatched}"
    )


def test_interlock_2b_the_officers_id_wins_over_the_models(dispatched):
    """The model naming another application must not move the tool off the officer's
    file. `create_agent` dispatches whatever args the message carries, so the pinning
    has to happen before the tool call leaves the seam."""
    client, _ = _client(SCORE_ELSEWHERE, FINAL)
    assistant.run(42, client)
    assert dispatched, "no tool ran"
    assert all(app_id == 42 for _, app_id in dispatched), (
        f"a tool ran against an id the model chose: {dispatched}"
    )


# --- interlock 3: the model-authored query never rides into a request ----------------


def test_interlock_3_the_policy_query_never_reaches_a_later_request(
    dispatched, monkeypatch
):
    monkeypatch.setitem(
        assistant._TOOLS,
        "search_policy",
        lambda query, task: (
            assistant.policy_retrieval.PolicyAnswer(
                status="policy_abstain",
                reason="below_threshold",
                score=0.11,
                chunk_id=None,
                text=None,
            )
            if hasattr(assistant.policy_retrieval, "PolicyAnswer")
            else None
        ),
    )
    client, adapter = _client(SEARCH, FINAL)
    assistant.run(42, client, task="explain")
    sent = _outbound(adapter)
    assert "DTI cap" not in sent, "the model-authored query rode into a later request"
    assert "640" not in sent


# --- interlock 4: exhaustion is a refusal, not a framework sentence ------------------


def test_interlock_4_step_exhaustion_refuses(dispatched):
    """MEASURED on langgraph 1.2.10: at its recursion limit `create_react_agent` does
    NOT raise. It appends `AIMessage('Sorry, need more steps to process this request.')`
    and returns normally — so a naive swap turns an intended refusal into either a 500
    (that sentence is not a valid action) or, worse, framework prose presented to an
    officer as an answer. The refusal has to be ours."""
    client, _ = _client(*([SEARCH] * (assistant._MAX_STEPS + 4)))
    with pytest.raises(assistant.AssistantError, match="no final answer"):
        assistant.run(42, client, task="explain")


def test_interlock_4_the_framework_sentence_never_reaches_the_officer(dispatched):
    client, _ = _client(*([SEARCH] * (assistant._MAX_STEPS + 4)))
    try:
        result = assistant.run(42, client, task="explain")
    except assistant.AssistantError as exc:
        assert "need more steps" not in str(exc)
    else:
        assert "need more steps" not in json.dumps(result), (
            "langgraph's canned recursion-limit sentence was served as an answer"
        )


def test_startup_hardens_the_trace_client():
    """The control has to be ON in a running service, not merely available. Runs the
    real lifespan (LLM feature off) and reads the singleton it claimed."""
    from fastapi.testclient import TestClient
    from langsmith.run_trees import get_cached_client

    from app.main import app

    with TestClient(app):
        client_ls = get_cached_client()
    assert client_ls._hide_inputs is True
    assert client_ls._hide_outputs is True
