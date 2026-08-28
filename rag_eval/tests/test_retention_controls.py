"""Retention controls the client set for the Titan run.

"No retention of query text, questions, retrieved content, identifiers,
credentials, or raw provider errors." Three writers in this harness retained some
of that: the report printed query text verbatim, the embedding cache wrote
content-derived vectors to disk, and a provider error escaped unwrapped (covered in
test_embedder_controls.py).

The client also named logging as a stop trigger, so a trace export is inside the
same rule as a log line — hence the tracing check.
"""

import pytest

from rag_eval.metrics import QueryEval
from rag_eval.report import _data_gaps_section, _metrics_section
from rag_eval.run import cache_enabled, refuse_traced_provider_run

SECRET = "How many days do we have to notify the applicant?"


def _eval(unanswerable: bool, retrieved: list[tuple[str, float]]) -> QueryEval:
    return QueryEval(
        query_id="Q01",
        query=SECRET,
        expected=[] if unanswerable else ["syn-pol-adverse-action#notification-timing"],
        unanswerable=unanswerable,
        retrieved=retrieved,
        threshold=0.16,
    )


def test_metrics_table_identifies_a_question_by_id_only():
    from rag_eval.metrics import aggregate

    evals = [_eval(False, [("syn-pol-adverse-action#notification-timing", 0.42)])]
    rows = "\n".join(
        _metrics_section(
            evals,
            aggregate(evals),
            threshold=0.16,
            embedder_signature="tfidf-v1:test",
            n_chunks=9,
        )
    )
    assert "Q01" in rows
    assert SECRET not in rows


def test_data_gaps_section_omits_query_text():
    # The false-confident section quoted the query directly, which is the one place
    # a supplied question would have been written out in full.
    rows = "\n".join(
        _data_gaps_section(
            [_eval(True, [("syn-pol-loan-review#counteroffers", 0.31)])], verdicts=[]
        )
    )
    assert "Q01" in rows
    assert SECRET not in rows


def _fake(provider: bool):
    class E:
        IS_PROVIDER_BACKED = provider

    return E()


def test_cache_disabled_for_a_provider_backend():
    # Cached dense vectors are provider-derived content sitting on disk. The
    # TF-IDF path is unaffected: it makes no provider call, and the local keyless
    # run depends on the cache to stay fast.
    assert cache_enabled(_fake(False)) is True
    assert cache_enabled(_fake(True)) is False


def test_traced_provider_run_is_refused(monkeypatch):
    # The LangSmith hardening blanks inputs and outputs but deliberately does NOT
    # hide errors, so a traced provider run can still export a failure. Nothing in
    # the client's required report needs traces, so the graded run refuses them.
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    with pytest.raises(ValueError, match="LANGSMITH_TRACING"):
        refuse_traced_provider_run(_fake(True))


def test_tracing_irrelevant_to_the_keyless_backend(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    refuse_traced_provider_run(_fake(False))  # no provider call, nothing to export


def test_untraced_provider_run_is_allowed(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    refuse_traced_provider_run(_fake(True))
