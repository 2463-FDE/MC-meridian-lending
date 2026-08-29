"""The offline evaluator: S-7's three controls, S-8/S-9/S-10, and C-6.

S-7 is three separate controls and each needs code, not intent (C-7): one
bounded pass, one retry for **technical** failure only, and no model fallback.
The client allows no retry for a bad *result*, so every control here is about
refusing rather than recovering.

Nothing in this file spends a real Bedrock call -- a fake client is injected,
the same way `BedrockEmbedder` takes one.
"""

from __future__ import annotations

import json

import pytest

from rag_eval.evaluator import (
    DEFAULT_JUDGE_MODEL,
    BedrockEvaluator,
    CaseVerdicts,
    EvaluatorProviderError,
    GradingCase,
)
from rag_eval.metrics import (
    ASSERTED,
    AVOIDED,
    HUMAN_REVIEW,
    SUPPORTED,
)


class _FakeBody:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload.encode()


class FakeClient:
    """Records every call and replays a scripted list of responses."""

    def __init__(self, *responses) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def invoke_model(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("the evaluator made more calls than were scripted")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return {"body": _FakeBody(nxt), "ResponseMetadata": {"RetryAttempts": 0}}


def _reply(conclusion=SUPPORTED, summary=SUPPORTED, prohibited=AVOIDED, rationale="ok"):
    """A well-formed Bedrock Anthropic-messages response body."""
    verdicts = {
        "conclusion_verdict": conclusion,
        "summary_verdict": summary,
        "prohibited_verdict": prohibited,
        "rationale": rationale,
    }
    return json.dumps({"content": [{"type": "text", "text": json.dumps(verdicts)}]})


def _case(query_id="q01"):
    return GradingCase(
        query_id=query_id,
        question="When must the notice go out?",
        passages=["A creditor shall notify an applicant within 30 days."],
        expected_conclusion="Within 30 days of a completed application.",
        displayed_summary="The officer is shown a 30-day deadline.",
        prohibited_conclusion="Quoting an invented numeric cutoff.",
    )


def _evaluator(*responses, **kw):
    return BedrockEvaluator(client=FakeClient(*responses), region="us-east-1", **kw)


# --- the happy path and the model contract ---------------------------------


def test_a_well_formed_reply_becomes_three_verdicts_and_one_line():
    ev = _evaluator(_reply(rationale="Names the 30-day deadline."))
    out = ev.grade(_case())
    assert out == CaseVerdicts(
        conclusion=SUPPORTED,
        summary=SUPPORTED,
        prohibited=AVOIDED,
        rationale="Names the 30-day deadline.",
    )


def test_the_request_pins_the_client_approved_model_and_zero_temperature():
    """Haiku 4.5 still accepts sampling parameters, so temperature 0 is
    actually settable here -- it does not make a generative judge
    deterministic, but it removes the cheapest source of variance, and S-7
    allows only one sample."""
    ev = _evaluator(_reply())
    ev.grade(_case())
    (call,) = ev._client.calls
    assert call["modelId"] == DEFAULT_JUDGE_MODEL
    body = json.loads(call["body"])
    assert body["temperature"] == 0
    assert body["anthropic_version"] == "bedrock-2023-05-31"


def test_the_model_id_is_a_dated_version_never_a_floating_alias():
    """S-7 gives one bounded pass. A model that moves under the run cannot be
    reported, and the report must state model ids."""
    assert DEFAULT_JUDGE_MODEL == "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_the_prompt_names_each_axis_by_its_own_state_words():
    """The polarity trap: two axes where `supported` is good share a prompt with
    one where `avoided` is. Asking for `supported` on the prohibited axis is how
    an inverted verdict gets produced, and S-7 allows no retry to fix one."""
    ev = _evaluator(_reply())
    ev.grade(_case())
    body = json.loads(ev._client.calls[0]["body"])
    prompt = body["system"] + json.dumps(body["messages"])
    prohibited_part = prompt.split("prohibited_verdict", 1)[1]
    assert AVOIDED in prohibited_part
    assert ASSERTED in prohibited_part


# --- S-7 control 1: one bounded pass ---------------------------------------


def test_grading_the_same_case_twice_is_refused():
    """One bounded pass. A caller that loops, or a retry written at the wrong
    level, would silently spend a second sample on a case already graded."""
    ev = _evaluator(_reply(), _reply())
    ev.grade(_case("q01"))
    with pytest.raises(RuntimeError, match="already graded"):
        ev.grade(_case("q01"))


def test_a_different_case_still_grades_after_one_is_spent():
    ev = _evaluator(_reply(), _reply())
    ev.grade(_case("q01"))
    assert ev.grade(_case("q02")).conclusion == SUPPORTED


# --- S-7 control 2: one retry, technical failure only ----------------------


def test_a_technical_failure_is_retried_exactly_once():
    ev = _evaluator(ConnectionError("socket reset"), _reply())
    assert ev.grade(_case()).conclusion == SUPPORTED
    assert len(ev._client.calls) == 2
    assert ev.retries == 1


def test_a_second_technical_failure_is_not_retried_again():
    ev = _evaluator(ConnectionError("one"), ConnectionError("two"))
    with pytest.raises(EvaluatorProviderError):
        ev.grade(_case())
    assert len(ev._client.calls) == 2


def test_an_unparseable_reply_is_human_review_not_a_retry():
    """An unparseable body is a bad RESULT, not a technical failure. S-7 allows
    no retry for a bad result, and S-9 sends what cannot be judged to human
    review rather than scoring it either way."""
    ev = _evaluator(json.dumps({"content": [{"type": "text", "text": "not json"}]}))
    out = ev.grade(_case())
    assert out.conclusion == HUMAN_REVIEW
    assert out.summary == HUMAN_REVIEW
    assert out.prohibited == HUMAN_REVIEW
    assert len(ev._client.calls) == 1


def test_a_verdict_outside_the_allowed_states_becomes_human_review():
    """A model that invents a state must not have it counted. Defaulting an
    unknown to `unsupported` would score "we could not tell" as "wrong"."""
    ev = _evaluator(_reply(conclusion="probably_fine"))
    out = ev.grade(_case())
    assert out.conclusion == HUMAN_REVIEW
    # The axes that DID come back valid are kept -- one bad field does not
    # discard a sample S-7 will not let us take again.
    assert out.summary == SUPPORTED
    assert out.prohibited == AVOIDED


def test_a_provider_returned_not_evaluated_becomes_human_review():
    """NOT_EVALUATED is the harness's absence-of-grade state (S-9), set before any
    call and moved only by a check that actually ran. A model that hands the
    literal back must not have it pass through as if it were a legitimate
    verdict -- that would make the absence state reachable through provider
    output, and S-7's one bounded pass means the case can never be re-graded to
    tell the two apart."""
    ev = _evaluator(_reply(conclusion="not_evaluated", prohibited="not_evaluated"))
    out = ev.grade(_case())
    assert out.conclusion == HUMAN_REVIEW
    assert out.prohibited == HUMAN_REVIEW


def test_a_support_state_on_the_prohibited_axis_is_refused():
    """`supported` is not in PROHIBITED_STATES. Accepting it would silently
    record the opposite of the finding."""
    ev = _evaluator(_reply(prohibited=SUPPORTED))
    assert ev.grade(_case()).prohibited == HUMAN_REVIEW


def test_a_prohibited_state_on_a_support_axis_is_refused():
    ev = _evaluator(_reply(conclusion=AVOIDED))
    assert ev.grade(_case()).conclusion == HUMAN_REVIEW


# --- S-7 control 3: no model fallback --------------------------------------


def _module_ast():
    import ast
    import inspect

    from rag_eval import evaluator as mod

    return ast.parse(inspect.getsource(mod))


def _string_constants(tree):
    """Every string literal in the module EXCEPT docstrings.

    Prose describes the controls; only code can break them, so the assertions
    below read the literals the interpreter would actually use.
    """
    import ast

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n.value not in docstrings
    ]


def test_there_is_no_fallback_model_to_fall_back_to():
    """Exactly one model id in the module's code. A second would silently grade
    part of the run on a model the report does not name."""
    ids = [s for s in _string_constants(_module_ast()) if "anthropic.claude" in s]
    assert ids == [DEFAULT_JUDGE_MODEL], f"a second model id is a fallback path: {ids}"


# --- S-10 retention and the provider-error path ----------------------------


def test_a_provider_error_never_carries_the_provider_text():
    """Same posture as EmbeddingProviderError: a provider body can echo the
    submitted passage, and the LangSmith hardening does not hide errors."""
    ev = _evaluator(
        ConnectionError("SECRET PASSAGE ECHO"), ConnectionError("SECRET PASSAGE ECHO")
    )
    with pytest.raises(EvaluatorProviderError) as caught:
        ev.grade(_case())
    assert "SECRET PASSAGE ECHO" not in str(caught.value)


def test_the_rationale_is_collapsed_to_one_line():
    """S-10 allows one line. Collapsing here means a multi-line reply does not
    arrive at the caller as a paragraph.

    The LENGTH cap is deliberately not enforced here -- it lives on the
    receiving field, so there is one limit rather than two that can drift. The
    step that wires this into `QueryEval` owns truncating to it, because that
    field refuses an overlong line and refusing would lose a case S-7 will not
    let us re-grade.
    """
    ev = _evaluator(_reply(rationale="  first\n\tsecond   third \n"))
    out = ev.grade(_case())
    assert out.rationale == "first second third"
    assert "\n" not in out.rationale


# --- tracing and C-6 --------------------------------------------------------


def test_a_traced_run_is_refused_before_any_call(monkeypatch):
    """`refuse_traced_provider_run` reads IS_PROVIDER_BACKED off the EMBEDDER
    and is blind to this client, so the evaluator needs its own refusal on the
    same variable. LangSmith stays off for the judge by decision."""
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    client = FakeClient(_reply())
    with pytest.raises(ValueError, match="LANGSMITH_TRACING"):
        BedrockEvaluator(client=client, region="us-east-1")
    assert client.calls == []


def test_a_blank_region_is_refused(monkeypatch):
    """Same control the embedder has: a discovered region silently changes which
    account grant the run depends on, and region probing is not approved."""
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    with pytest.raises(ValueError, match="region"):
        BedrockEvaluator(client=FakeClient(), region="  ")


def test_the_evaluator_imports_nothing_from_the_product_path():
    """C-6. `rag_eval/` already ships inside the origination image, so living in
    this package does not by itself keep the judge off the product path. Read
    off the import statements, not the prose that describes them."""
    import ast

    imported: list[str] = []
    for node in ast.walk(_module_ast()):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not [m for m in imported if m.split(".")[0] in {"services", "app"}], (
        f"the evaluator reaches onto the product path: {imported}"
    )


def test_nothing_runs_at_import_time_except_definitions():
    """The origination image imports `rag_eval`, so anything this module does at
    import time runs inside the service — a credential read most of all. Only
    imports, assignments and definitions may sit at module level."""
    import ast

    allowed = (
        ast.Import,
        ast.ImportFrom,
        ast.Assign,
        ast.AnnAssign,
        ast.ClassDef,
        ast.FunctionDef,
        ast.Expr,  # the module docstring
    )
    offenders = [
        type(n).__name__ for n in _module_ast().body if not isinstance(n, allowed)
    ]
    assert offenders == [], f"module-level statements that execute: {offenders}"
    # And no module-level assignment may read the environment.
    calls = [
        n
        for stmt in _module_ast().body
        if isinstance(stmt, (ast.Assign, ast.AnnAssign))
        for n in ast.walk(stmt)
        if isinstance(n, ast.Call)
    ]
    assert calls == [], "a module-level call runs at import time"
