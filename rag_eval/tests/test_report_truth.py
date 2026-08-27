"""The report must not state things that are false for the run it describes.

Three fixed strings in `report.py` were written against the 9-chunk corpus this
harness started on and the one denial (#6012) it was first pointed at. They are
emitted verbatim whatever the run actually was, so on the client's 66-chunk
corpus the report tells the reader the corpus is ~9 chunks and tells them, of
every false-confident retrieval, that the chunk "does not contain why this
specific application was denied" — for questions that are not about a denial.

The report is the graded run's deliverable. A false sentence in it is a defect
in the deliverable, not a cosmetic one.
"""

from __future__ import annotations

import pytest

from rag_eval import report as report_mod
from rag_eval.hygiene import FileVerdict
from rag_eval.metrics import QueryEval, aggregate
from rag_eval.run import _looks_like_person_name


def _eval(qid: str, query: str, *, unanswerable: bool, top: float) -> QueryEval:
    return QueryEval(
        query_id=qid,
        query=query,
        expected=[] if unanswerable else ["fees#late-fee"],
        unanswerable=unanswerable,
        retrieved=[("fees#late-fee", top)],
        threshold=0.10,
    )


def _report(evals: list[QueryEval], n_chunks: int) -> str:
    return report_mod.build(
        verdicts=[FileVerdict(path="policies/fees.md", passed=True, findings=[])],
        n_chunks=n_chunks,
        cache_hits=0,
        cache_misses=n_chunks,
        caching=True,
        provider_calls=0,
        provider_retries=0,
        provider_input_tokens=0,
        threshold=0.10,
        evals=evals,
        agg=aggregate(evals),
        embedder_signature="tfidf-v1:test",
    )


def test_calibration_note_states_the_real_corpus_size() -> None:
    """A 66-chunk run must not describe itself as a ~9-chunk corpus."""
    text = _report([_eval("q1", "What is the late fee?", unanswerable=False, top=0.5)], 66)
    assert "9-chunk" not in text, "report hardcodes the original corpus size"
    assert "66" in text


def test_false_confidence_note_does_not_assert_a_denial_question() -> None:
    """The per-row note is emitted for every false-confident case, whatever it asked."""
    ev = _eval("q21", "What is the origination fee?", unanswerable=True, top=0.9)
    text = _report([ev], 66)
    assert "**q21**" in text, "expected a false-confident row for this fixture"
    assert "why this specific application was denied" not in text, (
        "the note asserts the question was about a denial; q21 asks about a fee"
    )


@pytest.mark.parametrize(
    "phrase",
    ["Military Lending Act", "Servicemembers Civil Relief Act"],
)
def test_statutes_are_not_probable_person_names(phrase: str) -> None:
    """A statute is the false-positive class `_NAME_ALLOWLIST` exists to permit."""
    assert not _looks_like_person_name(
        f"Does the {phrase} cap the rate on this file?"
    )
