"""The two gold fields the evaluator grades: the conclusion and the summary it names.

The 08-27 contract requires `expected_conclusion` and `displayed_summary_id` on
all 28 rows, whatever the `outcome_class` — measured against the delivered
packet, where every row of `displayed-summaries.csv` carries a populated
`expected_conclusion_text` and `synthetic_displayed_summary`, the six `no_match`
rows included. A `no_match` case still has an expected conclusion (that no policy
in the corpus covers the question) and a summary shown to the officer, so both
are gradeable and S-1 wants them graded. An earlier draft of the contract
required the pair only on a non-`no_match` row and refused it on a `no_match`
one; that predicate rebuilds C-1, a loader that refuses her data.

"Required on all 28 rows" is enforced per FILE, not per row of every gold set in
the repository. A set carrying either field on any row must carry both on every
row. That refuses the incomplete v2 set the acceptance criterion aims at, and
leaves the committed 12-row set and the test fixtures — which predate the support
test and grade retrieval only — able to load unchanged.

`expected_conclusion` is her prose, so it inherits the `_UNECHOED_TEXT_KEYS`
treatment `prohibited_conclusion` established: never embedded, never reported
(S-10 allows counts, ids, the source-section reference and one rationale line),
and therefore outside the person-name heuristic, which guards text that is
embedded and echoed. Measured on her 28 rows, `expected_conclusion_text` trips
that heuristic on six.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rag_eval import run as run_mod

# Her Q08/Q10/Q12/Q15/Q18/Q24 shape: a conclusion whose Title-Case runs the
# person-name heuristic reads as a probable name.
HER_CONCLUSION_PROSE = (
    "The Credit Manager may not shorten the deadline, and the Credit Policy "
    "Schedule does not override it."
)


def _corpus(tmp_path: Path) -> Path:
    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "adverse-action.md").write_text(
        "# Adverse action\n\n## Notification timing\n\n"
        "Notify the applicant within 30 days after receiving the completed "
        "application.\n",
        encoding="utf-8",
    )
    return tmp_path


def _gold(base: Path, queries: list[dict]) -> Path:
    p = base / "gold.json"
    p.write_text(json.dumps({"queries": queries}), encoding="utf-8")
    return p


def _case(qid: str = "q01", **extra) -> dict:
    q = {
        "id": qid,
        "query": "How long is the notification deadline?",
        "source_document": "adverse-action.md",
        "source_heading": "Notification timing",
        "outcome_class": "answer",
        "support_literal": "30 days",
    }
    q.update(extra)
    return q


def _pair(**extra) -> dict:
    return _case(
        expected_conclusion=HER_CONCLUSION_PROSE,
        displayed_summary_id="Q01-ACCEPTABLE-CONCLUSION",
        **extra,
    )


def test_gold_accepts_the_pair_and_her_camelcase_conclusion(tmp_path: Path) -> None:
    """The authoritative JSONL spells it `acceptableConclusion`; take it as shipped."""
    base = _corpus(tmp_path)
    gold = _gold(
        base,
        [
            _case(
                acceptableConclusion=HER_CONCLUSION_PROSE,
                displayed_summary_id="Q01-ACCEPTABLE-CONCLUSION",
            )
        ],
    )
    result = run_mod.run(base=base, gold_path=gold)
    # Loaded, not refused. Nothing here reads the conclusion text — the verdict on
    # this row comes from the mechanical `support_literal` check, which is a
    # different input and stays untouched by the pair.
    assert [e.query_id for e in result.evals] == ["q01"]


def test_a_no_match_row_may_carry_the_pair(tmp_path: Path) -> None:
    """The regression test for the refused-her-data predicate (C-1).

    Six of her rows are `no_match`, and every one carries both fields. A loader
    that refuses the pair there drops 6 of 28 out of the support test.
    """
    base = _corpus(tmp_path)
    gold = _gold(
        base,
        [
            {
                # No anchor and no `expected`: an abstention case is scored on
                # staying below the threshold, so it names no chunk.
                "id": "q01",
                "query": "What is the payoff quote fee on a closed account?",
                "outcome_class": "no_match",
                "unanswerable": True,
                "expected_conclusion": "No policy in the corpus covers this question.",
                "displayed_summary_id": "Q11-ACCEPTABLE-CONCLUSION",
            }
        ],
    )
    result = run_mod.run(base=base, gold_path=gold)
    assert result.evals[0].unanswerable is True


def test_a_row_with_a_conclusion_but_no_summary_id_is_refused(tmp_path: Path) -> None:
    base = _corpus(tmp_path)
    gold = _gold(base, [_case(expected_conclusion=HER_CONCLUSION_PROSE)])
    with pytest.raises(RuntimeError, match="displayed_summary_id"):
        run_mod.run(base=base, gold_path=gold)


def test_a_row_with_a_summary_id_but_no_conclusion_is_refused(tmp_path: Path) -> None:
    base = _corpus(tmp_path)
    gold = _gold(base, [_case(displayed_summary_id="Q01-ACCEPTABLE-CONCLUSION")])
    with pytest.raises(RuntimeError, match="expected_conclusion"):
        run_mod.run(base=base, gold_path=gold)


def test_a_set_carrying_the_pair_must_carry_it_on_every_row(tmp_path: Path) -> None:
    """This is where "required on all 28 rows" is enforced.

    Per file, not per row of every gold set: a set that grades conclusions must
    grade all of them, or its rate describes a subset nobody chose.
    """
    base = _corpus(tmp_path)
    gold = _gold(base, [_pair(), _case(qid="q02")])
    with pytest.raises(RuntimeError, match="carries .* on some rows but not all"):
        run_mod.run(base=base, gold_path=gold)


def test_a_set_carrying_neither_field_still_loads(tmp_path: Path) -> None:
    """The committed 12-row set and the retrieval-only fixtures predate the pair."""
    base = _corpus(tmp_path)
    gold = _gold(base, [_case(), _case(qid="q02")])
    result = run_mod.run(base=base, gold_path=gold)
    assert len(result.evals) == 2


def test_her_conclusion_prose_does_not_trip_the_person_name_guard(
    tmp_path: Path,
) -> None:
    base = _corpus(tmp_path)
    gold = _gold(base, [_pair()])
    result = run_mod.run(base=base, gold_path=gold)
    assert [e.query_id for e in result.evals] == ["q01"]


def test_the_conclusion_text_never_reaches_the_report(tmp_path: Path) -> None:
    """S-10 allows counts, ids, the source reference and one rationale line."""
    base = _corpus(tmp_path)
    gold = _gold(base, [_pair()])
    result = run_mod.run(base=base, gold_path=gold)
    assert "Credit Manager" not in result.report_text
    assert HER_CONCLUSION_PROSE not in result.report_text


def test_the_conclusion_text_is_never_embedded(tmp_path: Path, monkeypatch) -> None:
    """Only the query goes to the provider."""
    seen: list[str] = []
    real = run_mod.make_embedder

    class Recorder:
        IS_PROVIDER_BACKED = False

        def __init__(self, inner):
            self._inner = inner

        def fit(self, corpus_texts):
            seen.extend(corpus_texts)
            return self._inner.fit(corpus_texts)

        def embed(self, text):
            seen.append(text)
            return self._inner.embed(text)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(run_mod, "make_embedder", lambda: Recorder(real()))
    base = _corpus(tmp_path)
    gold = _gold(base, [_pair()])
    run_mod.run(base=base, gold_path=gold)
    assert seen, "the recorder saw no text at all — the injection did not take"
    assert not any("Credit Manager" in t for t in seen)


def test_an_empty_conclusion_is_refused(tmp_path: Path) -> None:
    """A blank string is not a conclusion; it would grade as coverage it lacks."""
    base = _corpus(tmp_path)
    gold = _gold(
        base,
        [_case(expected_conclusion="   ", displayed_summary_id="Q01-X")],
    )
    with pytest.raises(RuntimeError, match="expected_conclusion"):
        run_mod.run(base=base, gold_path=gold)
