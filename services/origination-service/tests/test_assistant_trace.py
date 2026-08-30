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

import httpx
import pytest
from fastapi import HTTPException

from tests.test_native_script import native_adapter

from app import assistant, main, policy_retrieval
from app.llm import ClaudeClient, FakeAdapter, LLMConfig
from app.llm.errors import LLMError


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
        # Real `trace()` attaches `str(exception)` to the span an exception is raised
        # THROUGH, so a provider message or an app_id-bearing URL can reach LangSmith
        # without ever being put in `metadata`. Recorded so a test can assert the
        # entry span exits clean (`app/main.py::_run_assistant`).
        self.exception = None
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
            self.span.exception = exc[0] if exc else None
            stack.pop()
            return False

    monkeypatch.setattr(assistant, "trace", _Recorder)
    # `app/main.py` opens the entry span above the loop root, so both modules' `trace`
    # are recorded by one stack -- otherwise the parent/child shape the tests below
    # assert would be invisible.
    #
    # `raising=False` so this fixture does not decide the verdict. With it strict, a tree
    # where `main` has no `trace` at all makes every test below ERROR on the patch itself
    # -- which is `make prove` reporting red for "the attribute is missing" rather than
    # for "a refusal produced no root span". The claim under test is the span; let the
    # assertions be the ones that fail.
    monkeypatch.setattr(main, "trace", _Recorder, raising=False)
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
    """One loop root per run, covering the tools and the validation under it.

    Entry is covered by `assistant.entry` one level up (`app/main.py`); this test calls
    `run()` directly, so the loop root is the only root here.

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


# --- the entry span (app/main.py) --------------------------------------------------
#
# `assistant.request` opens inside `run()`, after the request is built, the policy topic
# is checked, the KYC gate runs and the application is fetched -- so every refusal raised
# before that point produced no trace at all. `assistant.entry` is the root that closes
# that gap, and the tests below pin both halves of why it is safe: what it carries, and
# that nothing is raised THROUGH it.


class _Boom(Exception):
    """Stands in for an exception `_run_assistant` does not catch."""


def _http_error(app_id: int) -> httpx.HTTPStatusError:
    """A downstream failure whose own message embeds the request URL, and so the app id.

    This is the leak the entry span is designed around: `str(exc)` names
    `/decisions/42/record`, and `trace()` would attach that string to any span the
    exception is raised through.
    """
    request = httpx.Request(
        "GET", f"http://decision-service:8004/decisions/{app_id}/record"
    )
    response = httpx.Response(500, request=request)
    return httpx.HTTPStatusError("server error", request=request, response=response)


def test_the_entry_span_is_the_root_and_parents_the_loop(spans, tools):
    main._run_assistant(42, _client(TOOL_CALL, FINAL_DENY), "decision")

    assert len(spans) == 1, f"expected one root, got {[s.name for s in spans]}"
    entry = spans[0]
    assert entry.name == "assistant.entry"
    assert [c.name for c in entry.children] == ["assistant.request"]


def test_the_entry_span_records_the_task_and_a_served_request(spans, tools):
    main._run_assistant(42, _client(TOOL_CALL, FINAL_DENY), "decision")

    entry = spans[0].metadata
    assert entry["task"] == "decision"
    assert entry["http_status"] == 200
    assert "refusal" not in entry, "a served request must not be marked refused"
    assert "policy_topic" not in entry, "absent, not null, when none was asked"


def test_the_entry_span_records_the_policy_topic_code(spans, tools, monkeypatch):
    monkeypatch.setitem(
        assistant._TOOLS,
        "search_policy",
        lambda query, task=None: {"status": "abstain"},
    )
    with pytest.raises(HTTPException):
        # An explain run that never searches is refused (PT-001); the topic is on the
        # entry span either way, which is the point -- a closed-vocabulary code says
        # what the officer asked about and carries nothing they typed.
        main._run_assistant(
            42,
            _client(FINAL_DENY),
            "explain",
            policy_topic="late_fee_waiver",
        )
    assert spans[0].metadata["policy_topic"] == "late_fee_waiver"


@pytest.mark.parametrize(
    "exc,status,refusal",
    [
        (assistant.ApplicationNotFound("never decisioned"), 404, "not_found"),
        (
            assistant.AssistantError("refusing an unrecorded decision"),
            502,
            "assistant_refused",
        ),
        (LLMError("provider said something raw"), 503, "llm_unavailable"),
    ],
)
def test_a_refusal_before_the_loop_still_produces_a_trace(
    spans, monkeypatch, exc, status, refusal
):
    """The gap this span closes: these three are all raised before `assistant.request`
    opens, so before this each one produced a trace with no root and no spans at all."""

    def _raise(*args, **kwargs):
        raise exc

    monkeypatch.setattr(assistant, "run", _raise)
    with pytest.raises(HTTPException) as raised:
        main._run_assistant(42, _client(), "decision")

    assert raised.value.status_code == status
    assert len(spans) == 1
    entry = spans[0]
    assert entry.name == "assistant.entry"
    assert entry.children == [], "the loop never opened its own root"
    assert entry.metadata["http_status"] == status
    assert entry.metadata["refusal"] == refusal


@pytest.mark.parametrize(
    "exc,status,refusal",
    [
        (assistant.ApplicationNotFound("never decisioned"), 404, "not_found"),
        (
            assistant.AssistantError("refusing an unrecorded decision"),
            502,
            "assistant_refused",
        ),
        (LLMError("provider said something raw"), 503, "llm_unavailable"),
        (_http_error(42), 503, "downstream_unavailable"),
    ],
)
def test_no_exception_is_raised_through_the_entry_span(
    spans, monkeypatch, exc, status, refusal
):
    """The whole reason the HTTPException is raised after the `with` block exits.

    `trace()` attaches `str(exception)` to the span it crosses, and two of these carry an
    application-linked string -- the httpx message embeds `/decisions/42/record`, and an
    LLMError can carry raw provider text. Translated to an enum inside the span, raised
    outside it."""

    def _raise(*args, **kwargs):
        raise exc

    monkeypatch.setattr(assistant, "run", _raise)
    with pytest.raises(HTTPException):
        main._run_assistant(42, _client(), "decision")

    entry = spans[0]
    assert entry.exception is None, (
        f"{entry.exception!r} crossed the entry span; trace() would ship str(exc)"
    )
    assert entry.metadata["refusal"] == refusal
    for key, value in entry.metadata.items():
        assert "42" not in str(value), f"{key}={value!r} carries the application id"
        assert "decisions/" not in str(value), f"{key}={value!r} carries a request URL"


def test_an_idempotency_conflict_is_its_own_refusal_code(spans, monkeypatch):
    request = httpx.Request("POST", "http://decision-service:8004/decisions")
    conflict = httpx.HTTPStatusError(
        "conflict", request=request, response=httpx.Response(409, request=request)
    )

    def _raise(*args, **kwargs):
        raise conflict

    monkeypatch.setattr(assistant, "run", _raise)
    with pytest.raises(HTTPException) as raised:
        main._run_assistant(42, _client(), "decision", "idem-key-1")

    assert raised.value.status_code == 409
    assert spans[0].metadata["refusal"] == "idempotency_conflict"


def test_an_uncaught_exception_is_not_translated_into_a_served_request(
    spans, monkeypatch
):
    """`_run_assistant` catches a named set; anything else must propagate as a 500 and
    must NOT reach `entry.add_metadata({"http_status": 200})`."""

    def _raise(*args, **kwargs):
        raise _Boom("unmapped")

    monkeypatch.setattr(assistant, "run", _raise)
    with pytest.raises(_Boom):
        main._run_assistant(42, _client(), "decision")

    assert "http_status" not in spans[0].metadata


def test_the_entry_span_never_carries_caller_linkable_identifiers(spans, tools):
    """Same rule as the loop root: no application_id, no request_id, on any span."""
    main._run_assistant(42, _client(TOOL_CALL, FINAL_DENY), "decision", "idem-key-1")

    for span in _flatten(spans):
        assert "application_id" not in span.metadata, span.name
        assert "request_id" not in span.metadata, span.name
        for key, value in span.metadata.items():
            assert "idem-key-1" not in str(value), f"{span.name}.{key}"


# --- refusals that reach the entry span from INSIDE the loop -------------------------
#
# The parametrized tests above stub `assistant.run` to raise, which pins the translation
# but not the propagation. These two drive the real score tool through the real graph,
# because both exception classes are raised inside it and langgraph's default ToolNode
# handler re-raises anything that is not a `ToolInvocationError` -- so they leave `run()`
# unchanged and land on the entry span's translation table.


@pytest.fixture
def scored_app(monkeypatch):
    """The score tool's real body, up to the call each test below makes fail."""
    monkeypatch.setattr(
        assistant, "decision_request_payload", lambda app_id: {"application_id": app_id}
    )


def test_a_kyc_refusal_is_translated_inside_the_entry_span(
    spans, scored_app, monkeypatch
):
    """ADR 0011's gate refuses with `HTTPException(409)` (app/kyc_gate.py::_block), not
    with one of the assistant's own classes, so an untranslated table exits the span as
    an exception and marks the run neither served nor refused."""

    def _blocked(app_id):
        raise HTTPException(
            status_code=409,
            detail="identity verification (KYC) has not passed for this application",
        )

    monkeypatch.setattr(assistant.kyc_gate, "require_kyc_passed", _blocked)

    with pytest.raises(HTTPException) as raised:
        main._run_assistant(42, _client(TOOL_CALL, FINAL_DENY), "decision")

    assert raised.value.status_code == 409
    assert "KYC" in raised.value.detail
    entry = spans[0]
    assert entry.exception is None, (
        f"{entry.exception!r} crossed the entry span; trace() would ship str(exc)"
    )
    assert entry.metadata["http_status"] == 409
    assert entry.metadata["refusal"] == "kyc_blocked"


def test_a_non_kyc_http_refusal_keeps_its_status_and_a_generic_code(spans, monkeypatch):
    """The KYC gate is the only `HTTPException` the assistant path raises today, so any
    other one gets its status honoured and a code that does not claim to be the gate's."""

    def _raise(*args, **kwargs):
        raise HTTPException(status_code=403, detail="refused upstream")

    monkeypatch.setattr(assistant, "run", _raise)
    with pytest.raises(HTTPException) as raised:
        main._run_assistant(42, _client(), "decision")

    assert raised.value.status_code == 403
    entry = spans[0]
    assert entry.exception is None
    assert entry.metadata["http_status"] == 403
    assert entry.metadata["refusal"] == "refused"


def test_a_transport_outage_is_a_downstream_refusal_not_a_500(
    spans, scored_app, monkeypatch
):
    """`httpx.RequestError` is not an `HTTPStatusError`: decision-service being
    unreachable is the same outage as it answering 500, and must refuse the same way
    rather than escaping untranslated as a 500."""

    def _unreachable(base_url, path, payload):
        raise httpx.ConnectError(
            "All connection attempts failed",
            request=httpx.Request("POST", f"{base_url}{path}"),
        )

    monkeypatch.setattr(assistant.clients, "post", _unreachable)

    with pytest.raises(HTTPException) as raised:
        main._run_assistant(42, _client(TOOL_CALL, FINAL_DENY), "decision")

    assert raised.value.status_code == 503
    entry = spans[0]
    assert entry.exception is None, (
        f"{entry.exception!r} crossed the entry span; trace() would ship str(exc)"
    )
    assert entry.metadata["http_status"] == 503
    assert entry.metadata["refusal"] == "downstream_unavailable"


# --- the outcome on the root ---------------------------------------------------------
#
# `assistant.request` carries the business outcome (app/assistant.py, the `root.add_metadata`
# block), and it is a CHILD of `assistant.entry`. LangSmith groups its charts by root-run
# metadata, so an outcome-mix or policy-band chart over the officer assistant had to filter
# child runs and could not sit beside cost, which rolls up to the root. These two tests pin
# the promotion and the allowlist that keeps it inside the CONTENT RULE.


# Every key the entry span may carry. The test below asserts the metadata is a SUBSET of
# this, so a future `**final` promotion -- `final` carries the officer summary, the verbatim
# citation text and the application id -- fails here rather than at the vendor.
_ENTRY_KEYS = {
    "task",
    "policy_topic",
    "http_status",
    "refusal",
    "outcome",
    "record_status",
    "policy_band",
    "narration_validated",
    "policy_citations",
    "policy_searches",
}


def test_the_entry_span_carries_the_outcome_for_charting(spans, tools):
    """The business outcome is on the ROOT, not only on the loop root beneath it."""
    main._run_assistant(42, _client(TOOL_CALL, FINAL_DENY), "decision")

    entry = spans[0].metadata
    assert entry["outcome"] == "deny"
    assert entry["record_status"] == "recorded"
    assert entry["policy_band"] == "deny"
    assert entry["narration_validated"] is True
    # Counts, never the lists: the citation objects carry verbatim corpus text.
    assert entry["policy_citations"] == 0
    assert entry["policy_searches"] == 0


def test_the_entry_span_promotes_counts_and_codes_but_no_content(spans, tools):
    """The promotion is an allowlist of enums, counts and one bool -- nothing from the
    officer-facing body. `run()` returns the summary, the application id and the citation
    text in the same dict, so spreading it would ship all three."""
    result = main._run_assistant(42, _client(TOOL_CALL, FINAL_DENY), "decision")

    entry = spans[0].metadata
    assert set(entry) <= _ENTRY_KEYS, f"unexpected keys: {set(entry) - _ENTRY_KEYS}"
    for key, value in entry.items():
        assert isinstance(value, (str, int, bool)), f"{key} carries {type(value)}"
    values = list(entry.values())
    assert result["summary"] not in values
    assert result["application_id"] not in values
    assert result["trace_id"] not in values


# --- refusals raised in the ROUTE, above `_run_assistant` -------------------------
#
# `_run_assistant` opens the entry span, so three post-authz business refusals in the
# route bodies above it produced no trace at all: the self-decision block, an overlong
# Idempotency-Key, and an unlisted policy_topic. Each is now raised through
# `main._refused_before_loop`, which opens the same `assistant.entry` root with its own
# enum code.
#
# `require_officer` and `check_llm_rate_limit` stay OUTSIDE the span deliberately, and
# `test_authz_and_rate_limit_refusals_stay_untraced` below pins that: a span opened
# before the rate limiter would let an unauthorized caller mint trace volume.


@pytest.fixture
def route_refusal(monkeypatch):
    """Neutralize everything the route touches except the refusal under test."""
    monkeypatch.setattr(main.authz, "require_officer", lambda role: None)
    monkeypatch.setattr(main.rate_limit, "check_llm_rate_limit", lambda uid: None)
    monkeypatch.setattr(main.authz, "deny_self_decision", lambda *a, **k: None)
    monkeypatch.setattr(main.assistant_runs, "record", lambda **kw: None)

    def _never(*args, **kwargs):
        raise AssertionError("the loop ran; the refusal was supposed to precede it")

    monkeypatch.setattr(assistant, "run", _never)


def test_the_self_decision_block_is_traced(spans, route_refusal, monkeypatch):
    def _blocked(app_id, role, uid):
        raise HTTPException(
            status_code=403,
            detail=(
                "a decision cannot be run by the applicant's own account; "
                "another officer must decision this application"
            ),
        )

    monkeypatch.setattr(main.authz, "deny_self_decision", _blocked)

    with pytest.raises(HTTPException) as raised:
        main.assistant_decide(
            42,
            idempotency_key=None,
            x_user_role="underwriter",
            x_user_id="u-1",
            client=_client(),
        )

    assert raised.value.status_code == 403
    assert len(spans) == 1
    entry = spans[0]
    assert entry.name == "assistant.entry"
    assert entry.metadata["task"] == "decision"
    assert entry.metadata["http_status"] == 403
    assert entry.metadata["refusal"] == "self_decision"
    assert entry.exception is None, (
        f"{entry.exception!r} crossed the entry span; trace() would ship str(exc)"
    )


def test_an_overlong_idempotency_key_is_traced(spans, route_refusal):
    with pytest.raises(HTTPException) as raised:
        main.assistant_decide(
            42,
            idempotency_key="k" * 65,
            x_user_role="underwriter",
            x_user_id="u-1",
            client=_client(),
        )

    assert raised.value.status_code == 400
    assert len(spans) == 1
    assert spans[0].metadata["refusal"] == "idempotency_key_too_long"
    assert spans[0].metadata["http_status"] == 400
    assert spans[0].exception is None


def test_an_unlisted_policy_topic_is_traced(spans, route_refusal):
    with pytest.raises(HTTPException) as raised:
        main.assistant_explain(
            42,
            policy_topic="not_a_real_topic",
            x_user_role="underwriter",
            x_user_id="u-1",
            client=_client(),
        )

    assert raised.value.status_code == 422
    assert len(spans) == 1
    entry = spans[0]
    assert entry.metadata["task"] == "explain"
    assert entry.metadata["refusal"] == "unknown_policy_topic"
    # The rejected topic is the officer's own input and never reaches the span: the
    # message names the vocabulary, the span names the code.
    assert "not_a_real_topic" not in json.dumps(entry.metadata)


def test_no_route_refusal_span_carries_an_identifier(spans, route_refusal):
    """Same rule the entry span already holds: no app_id, no user id, no key material."""
    with pytest.raises(HTTPException):
        main.assistant_decide(
            42,
            idempotency_key="Z" * 65,
            x_user_role="underwriter",
            x_user_id="user-9001",
            client=_client(),
        )

    blob = json.dumps(spans[0].metadata)
    for identifier in ("42", "user-9001", "ZZZ"):
        assert identifier not in blob, f"{identifier} reached the span"


@pytest.mark.parametrize(
    "attr,exc",
    [
        ("require_officer", HTTPException(status_code=403, detail="officer required")),
        (
            "check_llm_rate_limit",
            HTTPException(status_code=429, detail="rate limited"),
        ),
    ],
)
def test_authz_and_rate_limit_refusals_stay_untraced(
    spans, route_refusal, monkeypatch, attr, exc
):
    """Deliberate, not an oversight. These two run before the span opens because they are
    the controls standing between an arbitrary caller and this service: tracing them would
    hand whoever can reach the port a lever on trace volume, and the rate limiter is the
    thing that stops it. A refusal here is visible in the log, not in LangSmith."""
    module = main.authz if attr == "require_officer" else main.rate_limit

    def _refuse(*args, **kwargs):
        raise exc

    monkeypatch.setattr(module, attr, _refuse)

    with pytest.raises(HTTPException) as raised:
        main.assistant_decide(
            42,
            idempotency_key=None,
            x_user_role="borrower",
            x_user_id="u-1",
            client=_client(),
        )

    assert raised.value.status_code == exc.status_code
    assert spans == [], f"an unauthorized caller minted {[s.name for s in spans]}"
