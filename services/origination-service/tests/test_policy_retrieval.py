"""Policy retrieval on the assistant loop (ADR 0019).

Two halves. The retrieval module against the REAL committed corpus — abstention is the
default and every failure path reaches it — and the loop integration, which is where the
boundary lives: the officer receives the corpus text verbatim, the model receives a status
code and a number, and the model-authored query never crosses to the provider at all.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_native_script import native_adapter

from app import assistant, config, policy_retrieval
from app.llm import ClaudeClient, FakeAdapter, LLMConfig
from app.llm.request_builder import redact_json
from rag_eval.chunker import chunk_markdown

# Stable against the committed 9-chunk corpus: 0.346 and 0.274 respectively at the time of
# writing, both far above any threshold these tests set. `rag_eval` owns the real
# calibration (rag_eval/eval_report.md); these only need a hit and a miss.
ELIGIBILITY_QUERY = "minimum age eligibility"
ELIGIBILITY_CHUNK = "underwriting_guidelines#eligibility"
NONSENSE_QUERY = "what is the weather in reykjavik"


@pytest.fixture(autouse=True)
def _fresh_index():
    """No index state leaks between tests — each one configures its own corpus."""
    policy_retrieval.reset_index_cache()
    yield
    policy_retrieval.reset_index_cache()


@pytest.fixture
def threshold(monkeypatch):
    def _set(value):
        monkeypatch.setattr(config, "POLICY_RETRIEVAL_MIN_SCORE", value)

    return _set


# --- the retrieval module ----------------------------------------------------------


def test_unset_threshold_abstains_on_every_query(threshold):
    threshold("")
    answer = policy_retrieval.search(ELIGIBILITY_QUERY)
    assert answer.status == "policy_abstain"
    assert answer.reason == policy_retrieval.NO_THRESHOLD


@pytest.mark.parametrize("value", ["abc", "0", "0.0", "-0.2", "1.5", "  "])
def test_unusable_threshold_disables_retrieval_rather_than_guessing(threshold, value):
    """A typo must not become a permissive threshold.

    0 would quote the worst chunk in the corpus for any query, and >1 is unreachable for a
    cosine score, so both mean the operator did not configure this — not "admit everything".
    """
    threshold(value)
    assert config.policy_retrieval_min_score() is None
    assert policy_retrieval.search(ELIGIBILITY_QUERY).reason == (
        policy_retrieval.NO_THRESHOLD
    )


def test_empty_query_abstains(threshold):
    threshold("0.05")
    assert policy_retrieval.search("   ").reason == policy_retrieval.EMPTY_QUERY


def test_hit_returns_the_corpus_chunk_verbatim(threshold):
    """The officer-facing text must be the corpus bytes, not a rendering of them."""
    threshold("0.05")
    answer = policy_retrieval.search(ELIGIBILITY_QUERY)
    assert answer.is_hit and answer.chunk_id == ELIGIBILITY_CHUNK
    corpus = {
        c.chunk_id: c.text
        for c in chunk_markdown(
            policy_retrieval.corpus_dir() / "underwriting_guidelines.md"
        )
    }
    assert answer.text == corpus[ELIGIBILITY_CHUNK]


def test_weak_match_abstains_below_the_threshold(threshold):
    threshold("0.99")
    answer = policy_retrieval.search(ELIGIBILITY_QUERY)
    assert answer.status == "policy_abstain"
    assert answer.reason == policy_retrieval.BELOW_THRESHOLD
    assert 0.0 < answer.score < 0.99  # it matched something, just not well enough


def test_unrelated_question_abstains(threshold):
    threshold("0.05")
    assert policy_retrieval.search(NONSENSE_QUERY).status == "policy_abstain"


def test_tool_result_carries_no_corpus_text(threshold):
    threshold("0.05")
    answer = policy_retrieval.search(ELIGIBILITY_QUERY)
    assert answer.is_hit and answer.text  # the text exists...
    assert set(answer.tool_result()) == {
        "status",
        "score",
    }  # ...and does not go to the model


class _RecordingLog:
    """Captures what the module logs, formatted the way logging would render it."""

    def __init__(self):
        self.lines = []

    def _record(self, msg, *args):
        self.lines.append(msg % args if args else msg)

    warning = info = _record


def test_contaminated_corpus_file_is_refused_not_indexed(
    threshold, monkeypatch, tmp_path
):
    """ADR 0007's gate covers the committed corpus; this covers the one that arrives.

    The corpus reaches the container over a bind mount, so a misconfigured deployment can
    point it at a file the CI gate never saw. Fail closed on the file, keep the rest.
    """
    (tmp_path / "clean.md").write_text(
        "# Clean\n\n## Fees\n\nThe late fee is $35 flat.\n", encoding="utf-8"
    )
    (tmp_path / "dirty.md").write_text(
        "# Dirty\n\n## Case notes\n\nApplicant SSN 123-45-6789 called about fees.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "POLICY_CORPUS_DIR", str(tmp_path))
    threshold("0.05")
    # The module's own logger, captured directly: logging_config does not propagate to the
    # root logger caplog listens on, and its handler bound the real stderr at import.
    recorder = _RecordingLog()
    monkeypatch.setattr(policy_retrieval, "log", recorder)

    indexed = {chunk.doc for chunk in policy_retrieval._load_corpus()}
    assert indexed == {"clean"}, "the contaminated file must not be indexed"
    logged = "\n".join(recorder.lines)
    assert "REFUSED" in logged and "dirty.md" in logged
    assert "123-45-6789" not in logged  # the scan names the type, never the value


def test_unsafe_filename_is_refused_even_with_clean_content(
    threshold, monkeypatch, tmp_path
):
    """M1: scan_file only scans CONTENT. A file whose NAME carries an identity
    (a bind-mounted policies/jane-doe-123-45-6789.md, say) must not reach the
    chunker just because its prose is clean — the stem becomes the officer-
    visible chunk id (chunk_markdown, doc = path.stem)."""
    (tmp_path / "clean.md").write_text(
        "# Clean\n\n## Fees\n\nThe late fee is $35 flat.\n", encoding="utf-8"
    )
    (tmp_path / "jane-doe-123-45-6789.md").write_text(
        "# Fees\n\n## Late\n\nThe late fee is $35 flat.\n", encoding="utf-8"
    )
    monkeypatch.setattr(config, "POLICY_CORPUS_DIR", str(tmp_path))
    threshold("0.05")
    recorder = _RecordingLog()
    monkeypatch.setattr(policy_retrieval, "log", recorder)

    indexed = {chunk.doc for chunk in policy_retrieval._load_corpus()}
    assert indexed == {"clean"}, "the unsafe-named file must not be indexed"
    logged = "\n".join(recorder.lines)
    assert "REFUSED" in logged
    assert "jane-doe-123-45-6789" not in logged  # the name is the PII, never logged


def test_missing_corpus_directory_abstains(threshold, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "POLICY_CORPUS_DIR", str(tmp_path / "nope"))
    threshold("0.05")
    assert (
        policy_retrieval.search(ELIGIBILITY_QUERY).reason == policy_retrieval.NO_CORPUS
    )


# --- the loop --------------------------------------------------------------------


SEARCH_CALL = json.dumps(
    {
        "action": "tool",
        "tool": "search_policy",
        "input": {"query": ELIGIBILITY_QUERY},
    }
)
FINAL_EXPLAIN = json.dumps(
    {
        "action": "final",
        "outcome": "deny",
        "reason_codes": ["R02"],
        "summary": "The recorded decision is a denial; the policy passage is quoted below.",
    }
)
RECORD_BODY = {
    "application_id": 42,
    "status": "recorded",
    "outcome": "deny",
    "policy_band": "deny",
    "principal_reasons": [
        {"code": "R02", "reason": "Excessive obligations in relation to income"}
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
def record_seam(monkeypatch):
    monkeypatch.setattr(assistant.clients, "get", lambda base, path: _RecordResponse())


def _client(*responses):
    cfg = LLMConfig(
        api_key="test-key", max_retries=0, token_budget=20_000, max_tokens=256
    )
    adapter = native_adapter(*responses)
    return ClaudeClient(cfg, adapter=adapter), adapter


def test_explain_run_quotes_the_policy_to_the_officer(threshold, record_seam):
    threshold("0.05")
    client, _ = _client(SEARCH_CALL, FINAL_EXPLAIN)
    result = assistant.run(42, client, task="explain")

    corpus = {
        c.chunk_id: c.text
        for c in chunk_markdown(
            policy_retrieval.corpus_dir() / "underwriting_guidelines.md"
        )
    }
    citation = result["policy_citations"][0]
    assert citation["chunk_id"] == ELIGIBILITY_CHUNK
    assert citation["text"] == corpus[ELIGIBILITY_CHUNK]
    # In the summary too: the officer screen renders `summary`, so a citation that lived
    # only in the structured field would be retrieved and never read.
    assert corpus[ELIGIBILITY_CHUNK] in result["summary"]
    assert ELIGIBILITY_CHUNK in result["summary"]
    # The record-derived half is untouched by retrieval.
    assert result["outcome"] == "deny" and result["score"] == 518


def test_policy_query_never_reaches_the_provider(threshold, record_seam):
    """The query is model-authored free text. It is used in-process and dropped."""
    threshold("0.05")
    client, adapter = _client(SEARCH_CALL, FINAL_EXPLAIN)
    assistant.run(42, client, task="explain")
    # Serialized, not joined: under native tool calling a turn's content is a list of
    # tool_use / tool_result blocks, and a join over `str` would raise on the shape
    # rather than read it. This reads the whole outbound request either way, which is
    # also strictly more than the string content the earlier form saw.
    sent = json.dumps(adapter.calls[-1].messages)
    assert ELIGIBILITY_QUERY not in sent
    assert "policy_hit" in sent  # the status code did go back, unmasked


def test_retrieval_is_refused_on_the_decision_task(threshold, record_seam, monkeypatch):
    """Reg B: the corpus carries adverse-action guidance, so retrieval stays off the path
    that produces a regulated outcome (ADR 0019 decision 5)."""
    threshold("0.05")

    def _must_not_run(query):
        raise AssertionError("retrieval ran on a decision task")

    monkeypatch.setattr(policy_retrieval, "search", _must_not_run)
    monkeypatch.setitem(
        assistant._TOOLS, "score_application", lambda app_id, request_id=None: {}
    )
    client, adapter = _client(SEARCH_CALL, FINAL_EXPLAIN)
    result = assistant.run(42, client, task="decision")

    assert result["policy_citations"] == []
    sent = json.dumps(adapter.calls[-1].messages)
    assert "policy_abstain" in sent  # the model is told plainly that it got nothing


def test_no_citation_when_retrieval_abstains(threshold, record_seam):
    threshold("0.99")
    client, _ = _client(SEARCH_CALL, FINAL_EXPLAIN)
    result = assistant.run(42, client, task="explain")
    assert result["policy_citations"] == []
    assert "quoted verbatim" not in result["summary"]


def test_abstained_search_is_visible_and_distinct_from_no_search(
    threshold, record_seam
):
    """B1: an abstained search must not read the same as a run that never
    searched at all — the officer needs to see that retrieval was tried and
    came back empty, not just that no policy text is quoted."""
    threshold("0.99")
    client, _ = _client(SEARCH_CALL, FINAL_EXPLAIN)
    searched = assistant.run(42, client, task="explain")
    assert searched["policy_searches"] == [
        {
            "status": "policy_abstain",
            "score": searched["policy_searches"][0]["score"],
            "reason": policy_retrieval.BELOW_THRESHOLD,
        }
    ]
    assert "no passage matched above the required threshold" in searched["summary"]

    client, _ = _client(FINAL_EXPLAIN)
    never_searched = assistant.run(42, client, task="explain")
    assert never_searched["policy_searches"] == []
    assert never_searched["summary"] != searched["summary"]
    assert "Policy search" not in never_searched["summary"]


def test_repeated_hits_on_one_chunk_are_cited_once(threshold, record_seam):
    threshold("0.05")
    client, _ = _client(SEARCH_CALL, SEARCH_CALL, FINAL_EXPLAIN)
    result = assistant.run(42, client, task="explain")
    assert len(result["policy_citations"]) == 1


# --- the export contract is unchanged --------------------------------------------


def test_policy_status_codes_survive_the_redactor():
    payload = json.dumps(
        {"tool": "search_policy", "result": {"status": "policy_hit", "score": 0.3460}}
    )
    assert json.loads(redact_json(payload)) == json.loads(payload)


def test_corpus_prose_is_still_masked_by_the_redactor():
    """ADR 0019 adds no exemption. If this ever passes prose through, the decision to keep
    the policy text on the officer's side of the boundary has been undone somewhere else."""
    prose = "Minimum age: 18. US residency / valid SSN or ITIN required."
    masked = json.loads(
        redact_json(json.dumps({"status": "policy_hit", "text": prose}))
    )
    assert masked["status"] == "policy_hit"
    assert prose not in masked["text"]


# --- an absent harness is a disabled feature, not a dead service -------------------


def test_search_abstains_when_the_harness_is_not_importable(threshold, monkeypatch):
    """Same class as an unset threshold (`config.py`): retrieval off, service healthy."""
    threshold("0.05")
    monkeypatch.setattr(
        policy_retrieval,
        "_HARNESS_IMPORT_ERROR",
        ModuleNotFoundError("No module named 'rag_eval'"),
    )
    answer = policy_retrieval.search(ELIGIBILITY_QUERY)
    assert not answer.is_hit
    assert answer.reason == policy_retrieval.HARNESS_UNAVAILABLE
    assert answer.text == ""
    assert answer.chunk_id == ""


def test_search_abstains_when_the_embedder_cannot_be_built(threshold, monkeypatch):
    """A misconfigured embedder abstains; it does not 500 the officer's request.

    `make_embedder` reads `RAG_EMBEDDER`/`AWS_REGION` — both wired through
    `docker-compose.yml` — and raises `ValueError` on a typo'd backend name or an
    unusable Bedrock region. `_build_index` called it unguarded, so the exception
    left `search()`, crossed `assistant.entry` (app/main.py) and became a 500:
    the one class of retrieval failure that was NOT the documented abstention,
    while an unset threshold and an absent harness both were.
    """
    threshold("0.05")

    def _boom():
        raise ValueError("RAG_EMBEDDER='bedroc' is not one of ('tfidf', 'bedrock').")

    monkeypatch.setattr(policy_retrieval, "make_embedder", _boom)
    answer = policy_retrieval.search(ELIGIBILITY_QUERY)
    assert not answer.is_hit
    assert answer.reason == policy_retrieval.NO_CORPUS
    assert answer.text == ""
    assert answer.chunk_id == ""


def test_search_abstains_when_embedding_the_query_fails(threshold, monkeypatch):
    """The query embed is a provider call too, and the query is model-authored.

    A Bedrock fault here raised through `search()`; `assistant.entry` attaches
    `str(exception)` to its span, so an exception echoing the text it was handed
    would carry model-authored free text into the trace — the one thing this
    module promises never to log. Abstain, and record only the exception TYPE.
    """
    threshold("0.05")
    real = policy_retrieval.make_embedder

    def _fragile():
        embedder = real()
        fit, embed = embedder.fit, embedder.embed
        calls = {"n": 0}

        def _embed(text):
            calls["n"] += 1
            if calls["n"] > len(_corpus_chunk_ids()):
                raise RuntimeError(f"provider rejected input: {text}")
            return embed(text)

        embedder.fit, embedder.embed = fit, _embed
        return embedder

    monkeypatch.setattr(policy_retrieval, "make_embedder", _fragile)
    answer = policy_retrieval.search(ELIGIBILITY_QUERY)
    assert not answer.is_hit
    assert answer.reason == policy_retrieval.NO_CORPUS


def _corpus_chunk_ids():
    """Chunk ids the loader builds from the checkout corpus (indexing order)."""
    chunks = []
    for path in sorted(policy_retrieval.corpus_dir().glob("*.md")):
        chunks.extend(chunk_markdown(path))
    return [c.chunk_id for c in chunks]


def test_app_imports_without_the_repo_root_on_sys_path():
    """Reproduces CI's `backend` import smoke: `cd services/origination-service` then
    `python -c "import app.main"`. A bare interpreter never reads `pytest.ini`, so
    `pythonpath = ../..` does not apply and `rag_eval` is absent. Card G2a puts the
    harness in the IMAGE; it does not put it on a checkout's sys.path. The service must
    still import — the alternative is that installing the assistant's optional retrieval
    tool takes origination down wherever the repo root is not a package root.
    """
    service_dir = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=service_dir,
        env=env,
        capture_output=True,
        text=True,
    )
    assert "No module named 'rag_eval'" not in proc.stderr
    assert proc.returncode == 0, proc.stderr
