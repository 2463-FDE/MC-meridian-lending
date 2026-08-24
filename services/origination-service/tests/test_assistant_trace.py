"""The root trace over the officer assistant (freeze plan slice 3).

Before this the service emitted two spans, `llm.complete` and its `llm.transport` child,
so a trace showed that a model call happened and nothing about the agent that made it.
These tests pin the tree that now hangs above them, and — the larger half — pin what the
spans are allowed to carry.

The spans are asserted by recording what this module hands to `trace()`, not by talking
to LangSmith. That is deliberately the right level: the question these tests answer is
what WE put on a span, and a transport-level assertion would answer a different one while
needing a network.
"""

import json
import uuid

import pytest

from tests.test_native_script import native_adapter

from app import assistant, policy_retrieval
from app.llm import ClaudeClient, FakeAdapter, LLMConfig


@pytest.fixture(autouse=True)
def _kyc_passes(monkeypatch):
    monkeypatch.setattr(assistant.kyc_gate, "require_kyc_passed", lambda app_id: None)


class _Span:
    """One recorded span: its name, its run type, and everything attached to it."""

    def __init__(self, name, run_type, metadata):
        self.name = name
        self.run_type = run_type
        self.metadata = dict(metadata or {})
        self.children = []
        # Mirrors the real RunTree's `trace_id` field (app/assistant.py reads
        # `root.trace_id` for the officer-facing trace navigation), populated for every
        # span the same way LangSmith populates it regardless of run type.
        self.trace_id = uuid.uuid4()

    def add_metadata(self, metadata):
        self.metadata.update(metadata or {})

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<{self.name} {self.metadata}>"


@pytest.fixture
def spans(monkeypatch):
    """Record the span tree this module builds.

    Nesting is tracked with an explicit stack rather than inferred, because the shape is
    what half these tests assert: a tool span that is not a child of a step span would
    read as a tool the loop never ran.
    """
    roots, stack = [], []

    class _Recorder:
        def __init__(self, name, run_type="chain", metadata=None, **kwargs):
            self.span = _Span(name, run_type, metadata)

        def __enter__(self):
            (stack[-1].children if stack else roots).append(self.span)
            stack.append(self.span)
            return self.span

        def __exit__(self, *exc):
            stack.pop()
            return False

    monkeypatch.setattr(assistant, "trace", _Recorder)
    return roots


def _flatten(spans):
    for span in spans:
        yield span
        yield from _flatten(span.children)


def _named(spans, name):
    return [s for s in _flatten(spans) if s.name == name]


def _client(*responses):
    cfg = LLMConfig(
        api_key="test-key", max_retries=0, token_budget=20_000, max_tokens=256
    )
    return ClaudeClient(cfg, adapter=native_adapter(*responses))


TOOL_CALL = json.dumps(
    {"action": "tool", "tool": "score_application", "input": {"application_id": 42}}
)
SEARCH_CALL = json.dumps(
    {
        "action": "tool",
        "tool": "search_policy",
        "input": {"query": "late fee waiver rules"},
    }
)
FINAL_DENY = json.dumps(
    {
        "action": "final",
        "outcome": "deny",
        "reason_codes": ["R02"],
        "summary": "The application was denied: obligations are excessive.",
    }
)

SCORE_RESULT = {
    "status": "recorded",
    "outcome": "deny",
    "score": 518,
    "policy_band": "deny",
    "reason_codes": ["R02"],
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
        }
    ],
    "drivers": {"model_score": 518},
    "decided_by": "meridian-risk-stub:v1",
    "decided_at": "2026-07-15T12:00:00",
}


class _RecordResponse:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return RECORD_BODY


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.setitem(
        assistant._TOOLS,
        "score_application",
        lambda app_id, request_id=None: dict(SCORE_RESULT),
    )
    monkeypatch.setattr(assistant.clients, "get", lambda base, path: _RecordResponse())


# --- the tree ---------------------------------------------------------------------


def test_one_root_per_request_spanning_tool_and_validation(spans, tools):
    """The requirement is ONE root covering entry, tools and validation.

    There is no `assistant.step` span since the loop swap: the framework owns the loop,
    so its own node runs are the steps, and a hand-emitted span beside them would be a
    second name for one thing. What this service still owns hangs off the root directly
    — the tool dispatch and the record validation — in the order they ran.
    """
    assistant.run(42, _client(TOOL_CALL, FINAL_DENY))

    assert len(spans) == 1, (
        f"expected a single root span, got {[s.name for s in spans]}"
    )
    root = spans[0]
    assert root.name == "assistant.request"
    assert [c.name for c in root.children] == [
        "tool.score_application",
        "assistant.validate",
    ]
    assert not _named(spans, "assistant.step"), (
        "assistant.step was reintroduced beside the framework's own node runs"
    )


def test_the_tool_span_is_named_for_the_tool(spans, tools):
    assistant.run(42, _client(TOOL_CALL, FINAL_DENY))
    assert _named(spans, "tool.score_application")


def test_the_root_carries_the_business_outcome(spans, tools):
    assistant.run(42, _client(TOOL_CALL, FINAL_DENY))
    root = spans[0].metadata
    assert root["task"] == "decision"
    assert root["outcome"] == "deny"
    assert root["record_status"] == "recorded"
    assert root["policy_band"] == "deny"
    assert root["narration_validated"] is True
    assert root["steps_used"] == 2
    assert root["scored"] is True


def test_the_root_never_carries_caller_linkable_identifiers(spans, tools):
    """B1 (review): application_id/request_id are caller/applicant-linked
    identifiers -- same exposure class as the idempotency_key app/llm/client.py
    and app/llm/transport.py strip before tracing. Exporting either would make a
    LangSmith trace linkable to a specific customer record."""
    assistant.run(42, _client(TOOL_CALL, FINAL_DENY))
    root = spans[0].metadata
    assert "application_id" not in root
    assert "request_id" not in root


def test_the_response_carries_the_root_trace_id_for_officer_navigation(spans, tools):
    """Slice 6 (freeze week10): with no application_id/request_id left on any span
    (previous test), the officer's only way to open this run in LangSmith is the
    trace id itself -- so it must ride on the HTTP response, not just the span."""
    result = assistant.run(42, _client(TOOL_CALL, FINAL_DENY))
    assert result["trace_id"] == str(spans[0].trace_id)


def test_the_validation_span_records_the_control_working(spans, tools):
    """A trace that cannot show a narration diverging from the record cannot show the
    control working, so `narration_validated` is the span's reason to exist."""
    diverging = json.dumps(
        {
            "action": "final",
            "outcome": "approve",  # contradicts the recorded deny
            "reason_codes": [],
            "summary": "Approved.",
        }
    )
    result = assistant.run(42, _client(TOOL_CALL, diverging))

    validation = _named(spans, "assistant.validate")
    assert len(validation) == 1
    assert validation[0].metadata["narration_validated"] is False
    # And the recorded facts still win, which is the behaviour the span is reporting on.
    assert result["outcome"] == "deny"
    assert spans[0].metadata["narration_validated"] is False


def test_step_exhaustion_still_produces_a_root(spans, tools):
    """A refused run is exactly when a trace is worth having."""
    with pytest.raises(assistant.AssistantError):
        assistant.run(42, _client(*([TOOL_CALL] * assistant._MAX_STEPS)))
    assert len(spans) == 1
    # Bounded spend and one regulated decision — the two properties the budget exists
    # for. NOT an exact dispatch count: `create_react_agent` reserves a step for its
    # own soft stop, so a run that makes `_MAX_STEPS` model calls dispatches one tool
    # fewer, and pinning that number would pin the framework's step accounting rather
    # than our contract. The spans are also not all `score_application` — the seam
    # rewrites every repeat score request to `get_decision_record` (interlock 1), so
    # the trace shows one regulated decision and reads after it.
    tool_spans = [c for c in spans[0].children if c.name.startswith("tool.")]
    assert 1 <= len(tool_spans) <= assistant._MAX_STEPS, [c.name for c in tool_spans]
    assert len(_named(spans, "tool.score_application")) == 1
    # No outcome was reached, so none is claimed.
    assert "outcome" not in spans[0].metadata


# --- retrieval --------------------------------------------------------------------


def test_the_retrieval_span_carries_corpus_metadata_and_not_the_query(
    spans, monkeypatch, tools
):
    hit = policy_retrieval.PolicyAnswer(
        status="policy_hit",
        reason="",
        score=0.4213,
        chunk_id="fee_schedule#late-fee",
        text="A late fee is $35 flat, or 5% of the past-due amount, whichever is less.",
    )
    monkeypatch.setattr(policy_retrieval, "search", lambda query: hit)

    assistant.run(42, _client(SEARCH_CALL, FINAL_DENY), task="explain")

    retrieval = _named(spans, "policy.retrieval")
    assert len(retrieval) == 1
    meta = retrieval[0].metadata
    assert meta["status"] == "policy_hit"
    assert meta["score"] == 0.4213
    assert meta["chunk_id"] == "fee_schedule#late-fee"
    assert meta["refused_on_decision_task"] is False

    blob = json.dumps(meta)
    assert "late fee waiver rules" not in blob, (
        "the model-authored query reached a span"
    )
    assert "$35 flat" not in blob, "corpus text reached a span"


def test_a_decision_task_refusal_is_visible_on_the_retrieval_span(spans, tools):
    """The refusal is a compliance posture (ADR 0019 decision 5), so it belongs in the
    trace as a recorded fact rather than as an absent span."""
    assistant.run(42, _client(SEARCH_CALL, FINAL_DENY), task="decision")
    meta = _named(spans, "policy.retrieval")[0].metadata
    assert meta["refused_on_decision_task"] is True
    assert meta["status"] == "policy_abstain"
    assert meta["reason"] == policy_retrieval.DECISION_TASK
    assert meta["chunk_id"] is None


# --- the content rule -------------------------------------------------------------


def test_no_span_carries_the_credit_score(spans, tools):
    """Omitted on least-privilege grounds: it crosses to the provider already, inside the
    tool result the model reads, but it tells a trace reader nothing `outcome` and
    `policy_band` do not."""
    assistant.run(42, _client(TOOL_CALL, FINAL_DENY))
    for span in _flatten(spans):
        assert "score" not in span.metadata or span.name == "policy.retrieval", (
            f"{span.name} carries a score"
        )
        assert 518 not in span.metadata.values(), (
            f"{span.name} carries the credit score"
        )


def test_no_span_carries_applicant_content(spans, tools):
    """The whole design constraint: enums, integers, booleans, retrieval scores. Nothing
    from the applicant and nothing the model wrote."""
    assistant.run(42, _client(TOOL_CALL, FINAL_DENY))
    blob = json.dumps([s.metadata for s in _flatten(spans)])
    for leaked in (
        "Excessive obligations",  # the reason prose from the record
        "obligations are excessive",  # the model's narration
        "meridian-risk-stub",
        "2026-07-15T12:00:00",
    ):
        assert leaked not in blob, f"{leaked!r} reached a span"


def test_the_tool_span_carries_reason_codes_but_not_reason_prose(spans, tools):
    """Codes are a closed vocabulary (ADR 0009 §3); the reason text is borrower-facing
    prose drafted from the record."""
    monkeypatch_result = dict(SCORE_RESULT)
    monkeypatch_result["principal_reasons"] = RECORD_BODY["principal_reasons"]
    meta = assistant._result_metadata(monkeypatch_result)
    assert meta["reason_codes"] == ["R02"]
    assert "Excessive obligations in relation to income" not in json.dumps(meta)
    assert "score" not in meta


# --- _result_metadata is fed an unconstrained container ---------------------------


@pytest.mark.parametrize(
    "result",
    [
        None,
        "a string",
        42,
        ["a", "list"],
        {"status": {"nested": "object"}},
        {"status": ["a", "list"]},
        {"principal_reasons": "not a list"},
        {"principal_reasons": ["not a dict"]},
        {"principal_reasons": [{"code": 7}]},
        {"status": ""},
    ],
)
def test_result_metadata_type_checks_rather_than_trusting_the_shape(result):
    """A tool result is a dict today, but this projection must not index a container that
    is not one, nor stringify a value that is."""
    meta = assistant._result_metadata(result)
    assert isinstance(meta, dict)
    for value in meta.values():
        assert isinstance(value, (str, list)), (
            f"{value!r} is not an enum or a code list"
        )
        if isinstance(value, list):
            assert all(isinstance(v, str) for v in value)
