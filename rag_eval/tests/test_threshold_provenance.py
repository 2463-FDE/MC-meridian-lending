"""A threshold is only meaningful next to what it was calibrated against.

`POLICY_RETRIEVAL_MIN_SCORE=0.1609` was calibrated on a 9-chunk TF-IDF corpus.
At 66 chunks the same method yields 0.1367, and under a different embedding
backend it will move again — cosine scores are not comparable across either
change. A bare number in the report invites reuse of a value that no longer
means what it meant, so the report records the corpus and the embedder the
value belongs to.

The second half matters more for reading the result. On the client's corpus the
answerable and abstention score distributions overlap almost entirely: the
highest-scoring abstention case outranks every answerable case. No cutoff can
separate them, so the calibrated threshold is already optimal and the abstention
failures are a property of the corpus, not an untuned parameter. Reporting the
irreducible error count stops a reader treating a structural limit as a knob.
"""

from __future__ import annotations

from pathlib import Path

from rag_eval.run import calibrate_threshold, corpus_signature, threshold_errors
from rag_eval.chunker import Chunk


def _chunks(ids: list[str]) -> list[Chunk]:
    return [Chunk(chunk_id=i, doc=i.split("#")[0], section="s", text="t") for i in ids]


def test_corpus_signature_is_stable_and_content_addressed() -> None:
    a = _chunks(["d#one", "d#two"])
    assert corpus_signature(a) == corpus_signature(_chunks(["d#two", "d#one"])), (
        "signature must not depend on chunk ordering"
    )
    assert corpus_signature(a) != corpus_signature(_chunks(["d#one"]))


def test_threshold_errors_reports_both_directions() -> None:
    answerable = [0.5, 0.4]
    unanswerable = [0.45]
    t = calibrate_threshold(answerable, unanswerable)
    wrong_abstain, false_confident = threshold_errors(answerable, unanswerable, t)
    assert wrong_abstain + false_confident == 1, (
        "one of these two cases is inseparable; the count must show it"
    )


def test_calibrated_threshold_is_the_minimum_error_choice() -> None:
    """Guards the claim the report makes: this value is optimal, not arbitrary."""
    answerable = [0.30, 0.25, 0.20]
    unanswerable = [0.35, 0.10]
    t = calibrate_threshold(answerable, unanswerable)
    best = min(
        sum(threshold_errors(answerable, unanswerable, c))
        for c in [i / 1000 for i in range(0, 501)]
    )
    assert sum(threshold_errors(answerable, unanswerable, t)) == best


def test_report_names_the_corpus_and_embedder_the_threshold_belongs_to(
    tmp_path: Path,
) -> None:
    from rag_eval import run as run_mod
    import json

    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "fees.md").write_text(
        "# Fees\n\n## Late fee\n\nThe late payment fee is $35 flat.\n", encoding="utf-8"
    )
    gold = tmp_path / "gold.json"
    gold.write_text(
        json.dumps(
            {
                "queries": [
                    {
                        "id": "q01",
                        "query": "What is the late payment fee?",
                        "source_document": "fees.md",
                        "source_heading": "Late fee",
                        "outcome_class": "answer",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    result = run_mod.run(base=tmp_path, gold_path=gold)
    text = result.report_text
    assert "corpus-" in text, "report does not name the corpus the threshold belongs to"
    assert result.embedder_signature in text


def test_corpus_signature_moves_when_only_body_text_changes() -> None:
    """The signature has to bind content, not just the shape of the corpus.

    On the default non-manifest path a chunk id is the filename stem plus the
    section slug, so editing the body of `policies/fee_schedule.md` without
    renaming the file or its headings changes the embeddings — and potentially
    the right threshold — while leaving every id identical. A signature over ids
    alone would not move, and the report's "this threshold belongs to this
    corpus" claim would be false for exactly the edit most likely to happen.
    """
    before = [Chunk("fees#late-fee", "fees", "Late fee", "The late fee is $35 flat.")]
    after = [Chunk("fees#late-fee", "fees", "Late fee", "The late fee is $40 flat.")]
    assert corpus_signature(before) != corpus_signature(after), (
        "signature must move when body text changes under an unchanged id"
    )
    # The signature is printed in the report, and the client's retention limits
    # forbid retaining retrieved content: hash the text, never carry it.
    assert "35" not in corpus_signature(before)


def test_calibrate_threshold_searches_below_the_lowest_score() -> None:
    """Midpoints alone cannot reach the retrieve-everything cutoff.

    With one answerable at 0.2 under one abstention case at 0.3, the only
    midpoint (0.25) costs one wrong abstain plus one false confident. Any cutoff
    at or below 0.2 costs one error, not two — so the report's minimum-error
    claim needs that region in the candidate set.
    """
    answerable, unanswerable = [0.2], [0.3]
    t = calibrate_threshold(answerable, unanswerable)
    assert sum(threshold_errors(answerable, unanswerable, t)) == 1


def test_calibrate_threshold_searches_above_the_highest_score() -> None:
    """The mirror region: abstain-always is reachable only from above the max.

    One answerable at 0.1 under two abstention cases at 0.2 and 0.3 costs two
    errors at the best midpoint and one at any cutoff above 0.3.
    """
    answerable, unanswerable = [0.1], [0.2, 0.3]
    t = calibrate_threshold(answerable, unanswerable)
    assert sum(threshold_errors(answerable, unanswerable, t)) == 1
