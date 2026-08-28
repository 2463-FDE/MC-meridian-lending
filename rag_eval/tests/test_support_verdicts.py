"""Two support verdicts per case, graded separately and never merged.

The client's correction (S-1) is that the expected conclusion and the displayed
summary are two frozen targets, not one. A single boolean cannot carry them, and
merging them hides which half failed: a run that retrieves the right passage and
states the wrong deadline is not the same failure as one that states the right
deadline from the wrong passage.

S-9 adds a third state. An unsupported-or-uncertain verdict goes to human review
and counts neither way, because defaulting uncertainty to false would score
"we could not tell" as "the system was wrong" — the specific thing she forbade.

A fourth state, `not_evaluated`, is not a verdict but the absence of one. It
exists so this module cannot become what the design doc warns against: "a field
that is loaded but unscored reads as coverage that does not exist". Only the
mechanical cases are graded here — S-6 requires those to consume no model call —
and every other case says so in the report rather than defaulting to a pass.
"""

from __future__ import annotations

import json
from pathlib import Path

from rag_eval import run as run_mod
from rag_eval.metrics import (
    HUMAN_REVIEW,
    NOT_EVALUATED,
    SUPPORTED,
    UNSUPPORTED,
    QueryEval,
    aggregate,
)


def _corpus(tmp_path: Path) -> Path:
    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "adverse-action.md").write_text(
        "# Adverse action\n\n## Notification timing\n\n"
        "Notify the applicant within 30 days after receiving the completed "
        "application.\n\n## Record retention\n\n"
        "Keep consumer credit records for 25 months after notification.\n",
        encoding="utf-8",
    )
    return tmp_path


def _gold(base: Path, queries: list[dict]) -> Path:
    p = base / "gold.json"
    p.write_text(json.dumps({"queries": queries}), encoding="utf-8")
    return p


def _case(qid: str, heading: str, literal: str | None) -> dict:
    q = {
        "id": qid,
        "query": "How long is the notification deadline?",
        "source_document": "adverse-action.md",
        "source_heading": heading,
        "outcome_class": "answer",
    }
    if literal is not None:
        q["support_literal"] = literal
    return q


def test_a_supported_conclusion_is_graded_from_the_retrieved_passage(
    tmp_path: Path,
) -> None:
    base = _corpus(tmp_path)
    gold = _gold(base, [_case("q01", "Notification timing", "30 days")])
    result = run_mod.run(base=base, gold_path=gold)
    assert result.evals[0].conclusion_verdict == SUPPORTED


def test_a_literal_absent_from_the_passage_is_unsupported(tmp_path: Path) -> None:
    """The passage says 30 days; a conclusion asserting 45 is not supported by it."""
    base = _corpus(tmp_path)
    gold = _gold(base, [_case("q01", "Notification timing", "45 days")])
    result = run_mod.run(base=base, gold_path=gold)
    assert result.evals[0].conclusion_verdict == UNSUPPORTED


def test_a_case_with_no_literal_is_not_evaluated_rather_than_passed(
    tmp_path: Path,
) -> None:
    base = _corpus(tmp_path)
    gold = _gold(base, [_case("q01", "Notification timing", None)])
    result = run_mod.run(base=base, gold_path=gold)
    assert result.evals[0].conclusion_verdict == NOT_EVALUATED
    assert result.agg.conclusion_verdicts.get(SUPPORTED, 0) == 0, (
        "an ungraded case was counted as supported"
    )


def test_the_summary_verdict_is_independent_of_the_conclusion_verdict() -> None:
    """S-1: two targets, never merged into one number."""
    e = QueryEval(
        query_id="q1",
        query="q",
        expected=["d#s"],
        unanswerable=False,
        retrieved=[("d#s", 0.9)],
        threshold=0.1,
        conclusion_verdict=SUPPORTED,
        summary_verdict=UNSUPPORTED,
    )
    agg = aggregate([e])
    assert agg.conclusion_verdicts[SUPPORTED] == 1
    assert agg.summary_verdicts[UNSUPPORTED] == 1
    assert agg.conclusion_verdicts.get(UNSUPPORTED, 0) == 0


def test_human_review_counts_neither_way() -> None:
    """S-9: uncertainty must not be scored as failure."""
    evals = [
        QueryEval(
            query_id=f"q{i}",
            query="q",
            expected=["d#s"],
            unanswerable=False,
            retrieved=[("d#s", 0.9)],
            threshold=0.1,
            conclusion_verdict=v,
        )
        for i, v in enumerate((SUPPORTED, UNSUPPORTED, HUMAN_REVIEW))
    ]
    agg = aggregate(evals)
    assert agg.conclusion_verdicts[HUMAN_REVIEW] == 1
    assert agg.conclusion_verdicts[SUPPORTED] == 1
    assert agg.conclusion_verdicts[UNSUPPORTED] == 1


def test_report_shows_both_verdicts_separately_and_never_a_merged_score(
    tmp_path: Path,
) -> None:
    base = _corpus(tmp_path)
    gold = _gold(
        base,
        [
            _case("q01", "Notification timing", "30 days"),
            _case("q04", "Record retention", "25 months"),
        ],
    )
    result = run_mod.run(base=base, gold_path=gold)
    text = result.report_text
    assert "Expected conclusion" in text and "Displayed summary" in text
    assert "not evaluated" in text.lower()
