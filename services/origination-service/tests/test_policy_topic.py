"""`policy_topic` — the officer's channel into policy retrieval (ADR 0019).

Retrieval shipped, the loop could call it, and the model never did. Not a prompt-tuning
problem: the model-visible request was `{"application_id": N, "task": "explain"}` and
nothing more, so there was no policy question to answer. Adding a free-text `question`
field would not have worked either — `question` is not in `_SAFE_CATEGORICAL`, so the
boundary masks it and the model receives `"•••• (free text redacted)"`. Measured against
live Bedrock on 2026-08-23: identical action with and without a question, on both turns.

So the channel is a CODE from a closed vocabulary. An enum passes redaction intact
(same mechanism as `task`/`outcome`), it satisfies the span CONTENT RULE, and it cannot
carry anything an officer typed because there is nothing to type.

These tests cover the three places the vocabulary has to agree — the corpus it names, the
redaction allowlist, and the route — plus the plumbing that puts it in front of the model.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_native_script import native_adapter

from app import assistant, main, policy_retrieval
from app.llm import ClaudeClient, FakeAdapter, LLMConfig
from app.llm.request_builder import _SAFE_CATEGORICAL, redact_json

REPO = Path(__file__).resolve().parents[3]
CORPUS = " ".join(
    p.read_text().lower() for p in sorted((REPO / "policies").glob("*.md"))
)


# --- the vocabulary agrees with everything that depends on it -----------------


def test_vocabulary_matches_the_redaction_allowlist():
    """Duplicated rather than imported: `app/llm/` depends on nothing in the app domain,
    and importing `policy_retrieval` there would drag `rag_eval` into the redaction path.
    Duplication is only safe with a parity test — this is it."""
    assert set(policy_retrieval.POLICY_TOPICS) == _SAFE_CATEGORICAL["policy_topic"]


def test_every_topic_names_something_the_corpus_actually_covers():
    """A topic the corpus does not mention is an officer control that can only ever
    abstain. Asserted against the committed policy text rather than against retrieval,
    so it holds without `rag_eval` importable and without a calibrated threshold —
    `rag-eval-import-gate` covers real retrieval in the built image.

    Deliberately word-level, not a topic-to-chunk mapping: the model writes its own
    query from the topic, so pinning a chunk would over-specify what retrieval must
    return, while this still fails if a section is renamed or dropped."""
    missing = {}
    for topic in policy_retrieval.POLICY_TOPICS:
        absent = [w for w in topic.split("_") if len(w) > 3 and w not in CORPUS]
        if absent:
            missing[topic] = absent
    assert missing == {}, (
        f"topic word(s) absent from the committed corpus: {missing}. A topic the "
        f"corpus does not cover is a control that can only abstain."
    )


def test_every_topic_is_a_compound_token_that_cannot_be_a_bare_name():
    """`_SAFE_CATEGORICAL`'s standing requirement for any value it admits raw: a code
    that could pass for a person's name would let one through the boundary."""
    for topic in policy_retrieval.POLICY_TOPICS:
        assert "_" in topic, f"{topic!r} is a single token"
        assert topic.islower() and topic.replace("_", "").isalpha()


# --- redaction ----------------------------------------------------------------


def test_a_topic_code_reaches_the_model_intact():
    sent = redact_json(
        json.dumps({"task": "explain", "policy_topic": "debt_to_income"})
    )
    assert json.loads(sent)["policy_topic"] == "debt_to_income"


def test_a_topic_that_is_not_in_the_vocabulary_is_masked():
    """Defence in depth behind the route's 422: an unlisted value is not passed raw
    just because it sits under an allowlisted key."""
    sent = redact_json(
        json.dumps({"task": "explain", "policy_topic": "Jane Smith of 10 Main St"})
    )
    assert "Jane Smith" not in sent
    assert "Main St" not in sent


# --- the route ----------------------------------------------------------------


@pytest.fixture
def officer_client(monkeypatch):
    """A TestClient with the LLM feature on and the agent stubbed, so these assertions
    are about the route's contract rather than the loop."""
    calls = []

    def _fake_run(app_id, client, task, request_id=None, policy_topic=None):
        calls.append({"app_id": app_id, "task": task, "policy_topic": policy_topic})
        return {"application_id": app_id, "policy_citations": [], "summary": "ok"}

    monkeypatch.setattr(assistant, "run", _fake_run)
    # Override the dependency, never monkeypatch the attribute: the route captured the
    # original function object at import, so replacing `main.get_llm_client` would key
    # the override to the replacement and leave the real dependency (and its 503 for a
    # disabled feature) in the route.
    main.app.dependency_overrides[main.get_llm_client] = lambda: object()
    try:
        yield TestClient(main.app), calls
    finally:
        main.app.dependency_overrides.clear()


def _headers():
    return {"X-User-Role": "underwriter", "X-User-Id": "7"}


def test_a_valid_topic_reaches_the_loop(officer_client):
    client, calls = officer_client
    r = client.get(
        "/assistant/decisions/6013?policy_topic=debt_to_income", headers=_headers()
    )
    assert r.status_code == 200
    assert calls[-1]["policy_topic"] == "debt_to_income"


def test_omitting_the_topic_is_the_behaviour_the_route_always_had(officer_client):
    """Adding the parameter must take nothing away from a caller that does not send it."""
    client, calls = officer_client
    r = client.get("/assistant/decisions/6013", headers=_headers())
    assert r.status_code == 200
    assert calls[-1]["policy_topic"] is None


def test_an_unknown_topic_is_refused_with_the_vocabulary(officer_client):
    """422 here, not a masked value at the boundary: a masked topic produces a run that
    is indistinguishable from a genuine abstention, so the officer would be told 'no
    policy matched' when the real answer is 'that topic does not exist'."""
    client, calls = officer_client
    r = client.get(
        "/assistant/decisions/6013?policy_topic=late_fees", headers=_headers()
    )
    assert r.status_code == 422
    assert "debt_to_income" in r.json()["detail"]  # the vocabulary is in the message
    assert calls == []  # refused before the loop, so no model call is paid for


def test_the_topic_does_not_open_the_route_to_a_non_officer(officer_client):
    client, calls = officer_client
    r = client.get(
        "/assistant/decisions/6013?policy_topic=debt_to_income",
        headers={"X-User-Role": "borrower", "X-User-Id": "7"},
    )
    assert r.status_code in (401, 403)
    assert calls == []


# --- the loop -----------------------------------------------------------------

SEARCH_ACTION = json.dumps(
    {"action": "tool", "tool": "search_policy", "input": {"query": "dti limit"}}
)
FINAL = json.dumps(
    {
        "action": "final",
        "outcome": "refer",
        "reason_codes": ["R01"],
        "summary": "Referred for manual review; the policy passage is quoted below.",
    }
)


class _FakeRecord:
    """The decision-service record response, as `clients.get` returns it.

    `raise_for_status` is part of the contract, not decoration: `_validated_final`
    re-reads the record through this seam and checks the status before trusting it.
    """

    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return RECORD


RECORD = {
    "application_id": 42,
    "status": "recorded",
    "outcome": "refer",
    "policy_band": "refer",
    "principal_reasons": [
        {
            "code": "R01",
            "reason": "Delinquent past or present credit obligations with others",
            "feature": "delinquency_history",
        },
    ],
    "drivers": {"model_score": 636},
}


@pytest.fixture
def loop(monkeypatch):
    """Retrieval stubbed to a hit: this is about the topic reaching the model and the
    search being dispatched, not about scoring. Real retrieval is covered by
    `rag-eval-import-gate` in the built image."""
    monkeypatch.setattr(assistant.kyc_gate, "require_kyc_passed", lambda app_id: None)
    # Both HTTP seams, as `test_assistant.py::tools` does: the record tool reads
    # decision-service over `clients.get`, and `_validated_final` re-reads the record
    # through the same seam rather than trusting the model's account of it.
    monkeypatch.setitem(assistant._TOOLS, "get_decision_record", lambda _a: RECORD)
    monkeypatch.setattr(assistant.clients, "get", lambda base, path: _FakeRecord())
    monkeypatch.setattr(
        assistant.policy_retrieval,
        "search",
        lambda q: policy_retrieval.PolicyAnswer(
            status="policy_hit",
            score=0.53,
            chunk_id="underwriting_guidelines#debt-to-income-dti",
            text="DTI must not exceed 43%.",
            reason="",
        ),
    )


def _client(*responses):
    cfg = LLMConfig(api_key="k", max_retries=0, token_budget=20_000, max_tokens=256)
    adapter = native_adapter(*responses)
    return ClaudeClient(cfg, adapter=adapter), adapter


def test_the_topic_is_in_the_request_the_model_sees(loop):
    client, adapter = _client(SEARCH_ACTION, FINAL)
    assistant.run(42, client, "explain", policy_topic="debt_to_income")
    first_user_msg = adapter.calls[0].messages[-1]["content"]
    assert "debt_to_income" in first_user_msg


def test_a_searched_run_returns_the_citation_to_the_officer(loop):
    client, _ = _client(SEARCH_ACTION, FINAL)
    out = assistant.run(42, client, "explain", policy_topic="debt_to_income")
    assert out["policy_searches"], "the search must be recorded even when it hits"
    assert [c for c in out["policy_citations"]], "a hit must reach the officer"


def test_run_refuses_a_topic_outside_the_vocabulary(loop):
    """The route validates too, but `run()` has a second caller in this suite and an
    unlisted code would otherwise reach the model as a redaction placeholder."""
    client, adapter = _client(FINAL)
    with pytest.raises(assistant.AssistantError):
        assistant.run(42, client, "explain", policy_topic="not_a_topic")
    assert adapter.calls == []  # refused before the model is paid for


def test_a_final_without_a_search_is_refused_not_answered(loop):
    """PT-001: a model that skips search_policy entirely and goes straight to `final`
    must not reach the officer with a policy_topic request answered as if nothing was
    asked — that response is indistinguishable from a run that searched and abstained.
    Code must refuse it, not trust the model to have searched."""
    client, adapter = _client(FINAL)  # FINAL with no preceding SEARCH_ACTION
    with pytest.raises(assistant.AssistantError):
        assistant.run(42, client, "explain", policy_topic="debt_to_income")
    assert len(adapter.calls) == 1  # the model was paid for; the refusal is post-hoc


def test_a_second_search_is_ignored_not_re_run(loop, monkeypatch):
    """PT-001: search_policy is capped at one call per run — a model that searches
    twice must not compound retrieval calls or citations; the second call is answered
    from the cached first result, deterministically, without hitting retrieval again."""
    calls = []

    def _counting_search(q):
        calls.append(q)
        return policy_retrieval.PolicyAnswer(
            status="policy_hit",
            score=0.53,
            chunk_id="underwriting_guidelines#debt-to-income-dti",
            text="DTI must not exceed 43%.",
            reason="",
        )

    monkeypatch.setattr(assistant.policy_retrieval, "search", _counting_search)
    client, _ = _client(SEARCH_ACTION, SEARCH_ACTION, FINAL)
    out = assistant.run(42, client, "explain", policy_topic="debt_to_income")
    assert len(calls) == 1  # retrieval hit exactly once despite two tool calls
    assert len(out["policy_searches"]) == 1
    assert len(out["policy_citations"]) == 1
