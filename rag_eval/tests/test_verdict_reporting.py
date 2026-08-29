"""Per-topic verdict rates (S-4) and the one rationale line (S-8/S-10).

Two things the graded scope needs and the harness did not have. S-4 requires the
report broken out **by topic, not pooled**, and once the support verdicts are
inside the graded result that applies to them too -- until now only retrieval had
a per-topic breakout. S-8/S-10 allow the evaluator one rationale line per case,
and a field that is never validated is not a control (C-7).
"""

from __future__ import annotations

import pytest

from rag_eval.metrics import (
    ASSERTED,
    AVOIDED,
    HUMAN_REVIEW,
    NOT_EVALUATED,
    RATIONALE_MAX_CHARS,
    SUPPORTED,
    UNSCORABLE_CLASS,
    UNSUPPORTED,
    QueryEval,
    aggregate,
)


def _eval(query_id, topic, *, conclusion, summary, prohibited, **kw):
    """A retrieval-correct case, so a verdict assertion cannot be a retrieval one."""
    return QueryEval(
        query_id=query_id,
        query="q",
        expected=["c1"],
        unanswerable=False,
        retrieved=[("c1", 0.9)],
        threshold=0.5,
        topic=topic,
        conclusion_verdict=conclusion,
        summary_verdict=summary,
        prohibited_verdict=prohibited,
        **kw,
    )


# --- per-topic verdict rates (S-4) -----------------------------------------


def test_verdict_rates_are_broken_out_per_topic():
    evals = [
        _eval(
            "q1",
            "adverse_action",
            conclusion=SUPPORTED,
            summary=SUPPORTED,
            prohibited=AVOIDED,
        ),
        _eval(
            "q2",
            "adverse_action",
            conclusion=UNSUPPORTED,
            summary=SUPPORTED,
            prohibited=ASSERTED,
        ),
        _eval(
            "q3",
            "records_retention",
            conclusion=SUPPORTED,
            summary=UNSUPPORTED,
            prohibited=AVOIDED,
        ),
    ]
    agg = aggregate(evals)

    aa = agg.verdicts_by_topic["adverse_action"]
    assert aa.conclusion.n_graded == 2
    assert aa.conclusion.rate == pytest.approx(0.5)
    assert aa.summary.rate == pytest.approx(1.0)
    # The negative axis keeps its own polarity per topic, not the support one.
    assert aa.prohibited.counts[AVOIDED] == 1
    assert aa.prohibited.rate == pytest.approx(0.5)

    rr = agg.verdicts_by_topic["records_retention"]
    assert rr.conclusion.rate == pytest.approx(1.0)
    assert rr.summary.rate == pytest.approx(0.0)


def test_a_topic_whose_cases_are_all_ungraded_reports_no_rate_not_a_zero():
    """`n/a` and 0.00 mean opposite things; a pooled mean hid the difference."""
    agg = aggregate(
        [
            _eval(
                "q1",
                "fee_schedule",
                conclusion=NOT_EVALUATED,
                summary=NOT_EVALUATED,
                prohibited=NOT_EVALUATED,
            ),
        ]
    )
    fee = agg.verdicts_by_topic["fee_schedule"]
    assert fee.conclusion.n_graded == 0
    assert fee.conclusion.rate is None
    assert fee.prohibited.rate is None


def test_human_review_counts_in_no_per_topic_denominator():
    """S-9 holds per topic, not only in the pooled stat."""
    agg = aggregate(
        [
            _eval(
                "q1",
                "adverse_action",
                conclusion=SUPPORTED,
                summary=HUMAN_REVIEW,
                prohibited=AVOIDED,
            ),
            _eval(
                "q2",
                "adverse_action",
                conclusion=HUMAN_REVIEW,
                summary=SUPPORTED,
                prohibited=HUMAN_REVIEW,
            ),
        ]
    )
    aa = agg.verdicts_by_topic["adverse_action"]
    assert aa.conclusion.n_graded == 1
    assert aa.summary.n_graded == 1
    assert aa.prohibited.n_graded == 1


def test_an_unscorable_case_still_carries_a_verdict_into_its_topic():
    """A `clarification` case leaves every RETRIEVAL denominator but keeps its
    verdicts -- the pooled stats already count it, so the per-topic split must
    agree with them or the two disagree about the same run."""
    evals = [
        _eval(
            "q1",
            "adverse_action",
            conclusion=SUPPORTED,
            summary=SUPPORTED,
            prohibited=AVOIDED,
            outcome_class=UNSCORABLE_CLASS,
        ),
        _eval(
            "q2",
            "adverse_action",
            conclusion=UNSUPPORTED,
            summary=SUPPORTED,
            prohibited=AVOIDED,
        ),
    ]
    agg = aggregate(evals)

    # Retrieval drops it ...
    assert agg.by_topic["adverse_action"].n == 1
    # ... the verdict split does not, and it matches the pooled figure.
    assert agg.verdicts_by_topic["adverse_action"].n == 2
    assert agg.verdicts_by_topic["adverse_action"].conclusion.n_graded == 2
    assert agg.conclusion_verdicts.n_graded == 2


def test_per_topic_verdict_counts_sum_to_the_pooled_counts():
    """The two views are the same run; a split that loses a case is a bug."""
    evals = [
        _eval(
            "q1",
            "adverse_action",
            conclusion=SUPPORTED,
            summary=SUPPORTED,
            prohibited=AVOIDED,
        ),
        _eval(
            "q2",
            "records_retention",
            conclusion=UNSUPPORTED,
            summary=HUMAN_REVIEW,
            prohibited=ASSERTED,
        ),
        _eval(
            "q3",
            "fee_schedule",
            conclusion=NOT_EVALUATED,
            summary=NOT_EVALUATED,
            prohibited=NOT_EVALUATED,
        ),
    ]
    agg = aggregate(evals)
    assert sum(t.n for t in agg.verdicts_by_topic.values()) == len(evals)
    assert (
        sum(t.conclusion.n_graded for t in agg.verdicts_by_topic.values())
        == agg.conclusion_verdicts.n_graded
    )
    assert (
        sum(t.prohibited.n_graded for t in agg.verdicts_by_topic.values())
        == agg.prohibited_verdicts.n_graded
    )


# --- the rationale line (S-8 / S-10) ---------------------------------------


def test_rationale_defaults_to_empty_because_nothing_produces_one_yet():
    e = _eval(
        "q1",
        "adverse_action",
        conclusion=NOT_EVALUATED,
        summary=NOT_EVALUATED,
        prohibited=NOT_EVALUATED,
    )
    assert e.rationale == ""


def test_rationale_is_collapsed_to_one_line():
    """S-10 allows ONE line. A model that emits a paragraph must not widen the
    allowlist just by using newlines."""
    e = _eval(
        "q1",
        "adverse_action",
        conclusion=SUPPORTED,
        summary=SUPPORTED,
        prohibited=AVOIDED,
        rationale="  first\n\tsecond   third \n",
    )
    assert e.rationale == "first second third"
    assert "\n" not in e.rationale


def test_an_overlong_rationale_is_refused_rather_than_truncated():
    """A rationale long enough to carry a passage is the S-10 breach this guard
    exists for, and truncating one still leaks its first N characters."""
    with pytest.raises(ValueError, match="rationale"):
        _eval(
            "q1",
            "adverse_action",
            conclusion=SUPPORTED,
            summary=SUPPORTED,
            prohibited=AVOIDED,
            rationale="x" * (RATIONALE_MAX_CHARS + 1),
        )


def test_the_rationale_cap_is_shorter_than_a_corpus_chunk():
    """The cap only means something if a passage cannot fit inside it. Her
    chunks average ~475 chars; a cap at or above that buys nothing."""
    assert RATIONALE_MAX_CHARS < 400


# --- how the two land in the report ----------------------------------------


def test_the_per_topic_table_keeps_three_separate_rate_columns():
    """S-1 forbids a merged support number; merging them per topic is the same
    defect at a smaller scale."""
    from rag_eval.report import _verdicts_by_topic_table

    agg = aggregate(
        [
            _eval(
                "q1",
                "adverse_action",
                conclusion=SUPPORTED,
                summary=UNSUPPORTED,
                prohibited=AVOIDED,
            ),
        ]
    )
    table = "\n".join(_verdicts_by_topic_table(agg))
    assert (
        "| Topic | Cases | Expected conclusion | Displayed summary | Prohibited |"
        in table
    )
    # Conclusion 1.00, summary 0.00 -- a merged column could not show both.
    assert "1.00 (1 graded)" in table
    assert "0.00 (1 graded)" in table


def test_an_ungraded_topic_reads_n_a_not_zero_in_the_table():
    from rag_eval.report import _verdicts_by_topic_table

    agg = aggregate(
        [
            _eval(
                "q1",
                "interest_rate",
                conclusion=NOT_EVALUATED,
                summary=NOT_EVALUATED,
                prohibited=NOT_EVALUATED,
            ),
        ]
    )
    table = "\n".join(_verdicts_by_topic_table(agg))
    assert "n/a (0 graded)" in table
    assert "0.00" not in table


def test_unmapped_is_reported_beneath_the_table_never_as_a_topic_row():
    """Mirrors the retrieval table: a row beside real topics would read as
    coverage the closed officer vocabulary does not have."""
    from rag_eval.metrics import UNMAPPED
    from rag_eval.report import _verdicts_by_topic_table

    agg = aggregate(
        [
            _eval(
                "q1",
                "adverse_action",
                conclusion=SUPPORTED,
                summary=SUPPORTED,
                prohibited=AVOIDED,
            ),
            _eval(
                "q2",
                UNMAPPED,
                conclusion=SUPPORTED,
                summary=SUPPORTED,
                prohibited=AVOIDED,
            ),
        ]
    )
    table = "\n".join(_verdicts_by_topic_table(agg))
    assert f"| `{UNMAPPED}` |" not in table
    assert "1 case(s) carry `unmapped`" in table


def test_the_rationale_section_is_omitted_while_nothing_produces_one():
    """A column of blanks would read as an evaluator that ran and said nothing."""
    from rag_eval.report import _rationale_section

    evals = [
        _eval(
            "q1",
            "adverse_action",
            conclusion=NOT_EVALUATED,
            summary=NOT_EVALUATED,
            prohibited=NOT_EVALUATED,
        ),
    ]
    assert _rationale_section(evals) == []


def test_the_rationale_section_carries_only_allowlisted_fields():
    """S-10: case id, topic, the verdicts and one line -- never the conclusion,
    the summary or the passage."""
    from rag_eval.report import _rationale_section

    evals = [
        _eval(
            "q1",
            "adverse_action",
            conclusion=SUPPORTED,
            summary=SUPPORTED,
            prohibited=AVOIDED,
            rationale="Names the 30-day deadline.",
            expected_conclusion="SECRET CONCLUSION TEXT",
            prohibited_conclusion="SECRET PROHIBITED TEXT",
        ),
    ]
    section = "\n".join(_rationale_section(evals))
    assert "Names the 30-day deadline." in section
    assert "`q1`" in section
    assert "`adverse_action`" in section
    assert "SECRET CONCLUSION TEXT" not in section
    assert "SECRET PROHIBITED TEXT" not in section
