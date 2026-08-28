"""Gold set v2: an out-of-repo gold path, and the four outcome classes.

Two changes drive these tests, both from the client packet.

The gold set the client supplied cannot be committed. Her limits forbid retention
of question text and the repository is a public fork, so the questions have to be
readable from a working directory instead of from `rag_eval/gold_queries.json`.

Her questions carry four outcome classes where the harness knows two. `no_match`
maps onto the existing `unanswerable` scoring switch; `answer` and
`manager_escalation` map onto expected chunk ids; `clarification` is neither —
the answer exists but the question is under-specified, and the officer channel is
a closed topic enum so no ask-back path exists to score. It is reported, not
scored, and that has to be visible per class rather than hidden in an aggregate.

That last sentence is now enforced. This module originally scored
`clarification` beside the other classes and required it to name an expected
chunk. The client's delivered set refutes that: all five of her clarification
rows carry no `sourceDocument` — in the CSV and in the authoritative JSONL
alike — because they are ambiguous ACROSS documents by design. There is no
single anchor to score them against, so they are excluded from every rate and
reported as a count with the reason.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rag_eval import run as run_mod
from rag_eval.metrics import QueryEval, aggregate


def _corpus(tmp_path: Path) -> Path:
    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "fees.md").write_text(
        "# Fees\n\n## Late Fee\n\nLate payment fee is $35 flat.\n", encoding="utf-8"
    )
    return tmp_path


def _write_gold(path: Path, queries: list[dict]) -> Path:
    path.write_text(json.dumps({"queries": queries}), encoding="utf-8")
    return path


def test_run_reads_gold_from_supplied_path(tmp_path: Path, monkeypatch):
    # The committed gold set must not be the only source: the client's questions
    # live outside the repo and are named on the command line.
    base = _corpus(tmp_path)
    gold = _write_gold(
        tmp_path / "gold.json",
        [
            {
                "id": "q-late-fee",
                "query": "what is the late fee",
                "expected": ["fees#late-fee"],
            }
        ],
    )
    # If the supplied path were ignored, the committed set (12 queries against a
    # corpus not present here) would be scored instead.
    monkeypatch.setattr(run_mod, "GOLD_PATH", tmp_path / "does-not-exist.json")

    result = run_mod.run(base=base, gold_path=gold)

    assert [e.query_id for e in result.evals] == ["q-late-fee"]


def test_gold_path_defaults_to_the_committed_set(tmp_path: Path, monkeypatch):
    # Omitting the argument keeps the existing behaviour, so the CI gate and the
    # keyless structural run are unaffected.
    base = _corpus(tmp_path)
    gold = _write_gold(
        tmp_path / "committed.json",
        [
            {
                "id": "q-default",
                "query": "what is the late fee",
                "expected": ["fees#late-fee"],
            }
        ],
    )
    monkeypatch.setattr(run_mod, "GOLD_PATH", gold)

    result = run_mod.run(base=base)

    assert [e.query_id for e in result.evals] == ["q-default"]


def test_gold_accepts_outcome_class(tmp_path: Path, monkeypatch):
    base = _corpus(tmp_path)
    gold = _write_gold(
        tmp_path / "gold.json",
        [
            {
                "id": "q-answer",
                "query": "what is the late fee",
                "expected": ["fees#late-fee"],
                "outcome_class": "answer",
            },
            {
                "id": "q-clarify",
                "query": "what fee applies here",
                "outcome_class": "clarification",
            },
        ],
    )

    result = run_mod.run(base=base, gold_path=gold)

    assert {e.query_id for e in result.evals} == {"q-answer", "q-clarify"}


def test_gold_clarification_rejects_expected(tmp_path: Path, monkeypatch):
    # UNSCORABLE_CLASS rows are dropped from every denominator (aggregate()),
    # so a target here would be validated, resolved and printed while nothing
    # ever scores against it — the drift the anchor fields exist to prevent.
    base = _corpus(tmp_path)
    gold = _write_gold(
        tmp_path / "gold.json",
        [
            {
                "id": "q-clarify",
                "query": "what fee applies here",
                "expected": ["fees#late-fee"],
                "outcome_class": "clarification",
            }
        ],
    )

    with pytest.raises(RuntimeError, match="clarification"):
        run_mod.run(base=base, gold_path=gold)


def test_gold_clarification_rejects_anchor(tmp_path: Path, monkeypatch):
    base = _corpus(tmp_path)
    gold = _write_gold(
        tmp_path / "gold.json",
        [
            {
                "id": "q-clarify",
                "query": "what fee applies here",
                "sourceDocument": "fees.md",
                "sourceHeading": "Late Fee",
                "outcome_class": "clarification",
            }
        ],
    )

    with pytest.raises(RuntimeError, match="clarification"):
        run_mod.run(base=base, gold_path=gold)


def test_gold_rejects_unknown_outcome_class(tmp_path: Path, monkeypatch):
    # A free-text class would reappear as an unscored bucket nobody defined.
    base = _corpus(tmp_path)
    gold = _write_gold(
        tmp_path / "gold.json",
        [
            {
                "id": "q-bad",
                "query": "what is the late fee",
                "expected": [],
                "outcome_class": "escalate-to-legal",
            }
        ],
    )

    with pytest.raises(RuntimeError, match="outcome_class"):
        run_mod.run(base=base, gold_path=gold)


def test_gold_no_match_must_be_unanswerable(tmp_path: Path, monkeypatch):
    # `no_match` and `unanswerable` are the same fact in two fields. Letting them
    # disagree would score an abstention case as a retrieval case, silently.
    base = _corpus(tmp_path)
    gold = _write_gold(
        tmp_path / "gold.json",
        [
            {
                "id": "q-nomatch",
                "query": "what is the interest rate",
                "expected": [],
                "outcome_class": "no_match",
            }
        ],
    )

    with pytest.raises(RuntimeError, match="no_match"):
        run_mod.run(base=base, gold_path=gold)


def test_gold_unanswerable_must_be_no_match(tmp_path: Path, monkeypatch):
    base = _corpus(tmp_path)
    gold = _write_gold(
        tmp_path / "gold.json",
        [
            {
                "id": "q-mixed",
                "query": "what is the late fee",
                "expected": [],
                "unanswerable": True,
                "outcome_class": "answer",
            }
        ],
    )

    with pytest.raises(RuntimeError, match="no_match"):
        run_mod.run(base=base, gold_path=gold)


def test_aggregate_breaks_down_by_outcome_class():
    # 40 of the packet's 66 chunks are scaffolding repeated across five
    # documents, so an aggregate hit rate can look healthy while a whole class is
    # wrong. The breakdown is what makes that visible.
    evals = [
        QueryEval(
            query_id="a1",
            query="q",
            expected=["d#s"],
            unanswerable=False,
            retrieved=[("d#s", 0.9)],
            threshold=0.5,
            outcome_class="answer",
        ),
        QueryEval(
            query_id="a2",
            query="q",
            expected=["d#s"],
            unanswerable=False,
            retrieved=[("d#other", 0.9)],
            threshold=0.5,
            outcome_class="answer",
        ),
        QueryEval(
            query_id="c1",
            query="q",
            expected=["d#s"],
            unanswerable=False,
            retrieved=[("d#s", 0.9)],
            threshold=0.5,
            outcome_class="clarification",
        ),
        QueryEval(
            query_id="n1",
            query="q",
            expected=[],
            unanswerable=True,
            retrieved=[("d#s", 0.1)],
            threshold=0.5,
            outcome_class="no_match",
        ),
    ]

    agg = aggregate(evals)

    assert agg.by_class["answer"].n == 2
    assert agg.by_class["answer"].correct == 1
    assert "clarification" not in agg.by_class, (
        "an unscorable class must not appear as a scored row"
    )
    assert agg.n_unscorable == 1
    assert agg.by_class["no_match"].n == 1
    assert agg.by_class["no_match"].correct == 1


def test_report_renders_the_per_class_breakdown(tmp_path: Path, monkeypatch):
    # A metric computed but rendered nowhere reads as coverage that does not
    # exist. The breakdown has to reach the report the client is sent.
    base = _corpus(tmp_path)
    gold = _write_gold(
        tmp_path / "gold.json",
        [
            {
                "id": "q-answer",
                "query": "what is the late fee",
                "expected": ["fees#late-fee"],
                "outcome_class": "answer",
            },
            {
                "id": "q-clarify",
                "query": "which fee applies",
                "outcome_class": "clarification",
            },
        ],
    )

    result = run_mod.run(base=base, gold_path=gold)

    assert "| Outcome class | Cases | Correct |" in result.report_text
    assert "| `answer` | 1 |" in result.report_text
    assert "| `clarification` | 1 |" not in result.report_text
    assert "1 case(s) are `clarification`" in result.report_text


def test_outcome_class_defaults_when_absent():
    # The committed 12-query set carries no outcome_class. It must keep scoring
    # exactly as before rather than becoming an unclassified bucket.
    e = QueryEval(
        query_id="q",
        query="q",
        expected=["d#s"],
        unanswerable=False,
        retrieved=[("d#s", 0.9)],
        threshold=0.5,
    )

    assert e.outcome_class == "answer"

    u = QueryEval(
        query_id="u",
        query="q",
        expected=[],
        unanswerable=True,
        retrieved=[],
        threshold=0.5,
    )

    assert u.outcome_class == "no_match"


@pytest.mark.parametrize(
    "query",
    [
        {"id": "q-answer", "query": "what is the late fee", "outcome_class": "answer"},
        {
            "id": "q-escalate",
            "query": "what is the late fee",
            "expected": [],
            "outcome_class": "manager_escalation",
        },
        # No class and not unanswerable: resolves to `answer`, same rule.
        {"id": "q-implicit", "query": "what is the late fee", "expected": []},
    ],
)
def test_scored_class_requires_expected_chunk_ids(query: dict, tmp_path: Path):
    # A scored class with no expected chunk is unhittable: it scores incorrect
    # however retrieval behaves, and the report prints its empty expected cell
    # as "(unanswerable)" — the opposite of the class the case declares, in the
    # per-class table added to make class-level failure visible.
    base = _corpus(tmp_path)
    gold = _write_gold(tmp_path / "gold.json", [query])

    with pytest.raises(RuntimeError, match="no 'expected' chunk ids"):
        run_mod.run(base=base, gold_path=gold)


@pytest.mark.parametrize(
    "query",
    [
        {
            "id": "q-nomatch",
            "query": "what is the interest rate",
            "expected": ["fees#late-fee"],
            "unanswerable": True,
            "outcome_class": "no_match",
        },
        # No class but unanswerable: resolves to `no_match`, same rule.
        {
            "id": "q-implicit-nomatch",
            "query": "what is the interest rate",
            "expected": ["fees#late-fee"],
            "unanswerable": True,
        },
    ],
)
def test_no_match_must_not_carry_expected_chunk_ids(query: dict, tmp_path: Path):
    # An abstention case is scored on staying below the threshold; `expected` is
    # never read, so chunk ids there are printed in the report as the case's
    # expectation while nothing scores against them.
    base = _corpus(tmp_path)
    gold = _write_gold(tmp_path / "gold.json", [query])

    with pytest.raises(RuntimeError, match="must leave 'expected' empty"):
        run_mod.run(base=base, gold_path=gold)


@pytest.mark.parametrize("value", [["answer"], {"class": "answer"}])
def test_non_string_outcome_class_fails_as_a_schema_error(value, tmp_path: Path):
    # A JSON array/object is unhashable: testing it against the closed set
    # raises a raw TypeError instead of the schema error every other malformed
    # gold field fails with.
    base = _corpus(tmp_path)
    gold = _write_gold(
        tmp_path / "gold.json",
        [
            {
                "id": "q-bad-type",
                "query": "what is the late fee",
                "expected": ["fees#late-fee"],
                "outcome_class": value,
            }
        ],
    )

    with pytest.raises(RuntimeError, match="unknown 'outcome_class'"):
        run_mod.run(base=base, gold_path=gold)


def test_report_does_not_label_a_scored_case_unanswerable():
    # The empty-expected cell used to read "(unanswerable)" for any case with no
    # expected chunk, which labelled a scored class as the one class it is not.
    from rag_eval import report as report_mod
    from rag_eval.metrics import aggregate

    e = QueryEval(
        query_id="q-scored",
        query="q",
        expected=[],
        unanswerable=False,
        retrieved=[("d#s", 0.9)],
        threshold=0.5,
        outcome_class="answer",
    )

    text = report_mod.build(
        verdicts=[],
        n_chunks=1,
        cache_hits=0,
        cache_misses=0,
        # The TF-IDF shape: a cached run reports cache counters, so the provider
        # counters are the zeros run() passes for that backend.
        caching=True,
        provider_calls=0,
        provider_retries=0,
        provider_input_tokens=0,
        evals=[e],
        agg=aggregate([e]),
        threshold=0.5,
        embedder_signature="test",
        corpus_signature="corpus-test",
        wrong_abstain=0,
        false_confident=0,
    )

    assert "q-scored" in text
    assert "*(unanswerable)*" not in text
