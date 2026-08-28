"""The third grading target: the conclusion the run must NOT reach.

The client's authoritative JSONL carries `prohibitedUnsupportedConclusion` on
every one of her 28 rows, and the plan never counted it. It is an explicit
negative — Q13 names "answering with an invented cutoff or a single catch-all
denial script" — so it is not a third flavour of support. A conclusion can be
unsupported without being prohibited, and a prohibited conclusion is a distinct
failure from a wrong deadline read out of the right passage.

That is why the verdict states are their own (`avoided` / `asserted`) rather
than reusing SUPPORTED/UNSUPPORTED. "The prohibited conclusion is supported"
reads as the opposite of what it would mean, and this table goes into her
report.

Two constraints the field inherits and this module pins:

- **S-10 allows counts only.** The prohibited text must never reach the report.
- **It is never embedded.** Only the query is sent to the provider, so the
  field carries no provider exposure — which is what lets it sit out the
  free-text person-name heuristic the way an anchor does. Her own prose trips
  that heuristic on Q14 ("If Meridian ..."): the allowlist is phrase
  replacement, so the `Meridian Lending` entry cannot cover a bare `Meridian`
  after a sentence-initial capital, and adding entries will not close it.
"""

from __future__ import annotations

import json
from pathlib import Path

from rag_eval import run as run_mod
from rag_eval.metrics import (
    ASSERTED,
    AVOIDED,
    NOT_EVALUATED,
    PROHIBITED_STATES,
    QueryEval,
    aggregate,
)

# Her Q14 shape: prose that the person-name heuristic reads as a probable name.
# `Credit Manager` and `Credit Policy` are two Title-Case words each; `If
# Meridian` is a sentence-initial capital followed by the lender's bare name.
HER_PROHIBITED_PROSE = (
    "If Meridian applies a shorter private deadline, or the Credit Manager "
    "waives the Credit Policy Schedule, that is not supported."
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


def _case(**extra) -> dict:
    q = {
        "id": "q01",
        "query": "How long is the notification deadline?",
        "source_document": "adverse-action.md",
        "source_heading": "Notification timing",
        "outcome_class": "answer",
        "support_literal": "30 days",
    }
    q.update(extra)
    return q


def test_gold_accepts_her_camelcase_prohibited_conclusion(tmp_path: Path) -> None:
    """The authoritative JSONL spells it camelCase; accept it as shipped."""
    base = _corpus(tmp_path)
    gold = _gold(base, [_case(prohibitedUnsupportedConclusion="Any invented cutoff.")])
    result = run_mod.run(base=base, gold_path=gold)
    assert result.evals[0].prohibited_verdict == NOT_EVALUATED


def test_her_prohibited_prose_does_not_trip_the_person_name_guard(
    tmp_path: Path,
) -> None:
    """The guard exists for text that is embedded and echoed. This is neither.

    Before this field sat out the heuristic, her own Q14 wording refused the
    whole run, and no allowlist entry could fix it.
    """
    base = _corpus(tmp_path)
    gold = _gold(base, [_case(prohibited_conclusion=HER_PROHIBITED_PROSE)])
    result = run_mod.run(base=base, gold_path=gold)
    assert result.evals[0].prohibited_verdict == NOT_EVALUATED


def test_the_prohibited_text_never_reaches_the_report(tmp_path: Path) -> None:
    """S-10's retention allowlist is counts, ids and one rationale line."""
    base = _corpus(tmp_path)
    gold = _gold(base, [_case(prohibited_conclusion=HER_PROHIBITED_PROSE)])
    result = run_mod.run(base=base, gold_path=gold)
    assert "Meridian" not in result.report_text
    assert "Credit Manager" not in result.report_text
    assert HER_PROHIBITED_PROSE not in result.report_text


def test_the_prohibited_text_is_never_embedded(tmp_path: Path, monkeypatch) -> None:
    """Only the query goes to the provider. A leak here would bill and expose it."""
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
            # Everything the run reads off an embedder that is not a text call
            # (its signature, its provider counters) passes straight through.
            return getattr(self._inner, name)

    monkeypatch.setattr(run_mod, "make_embedder", lambda: Recorder(real()))
    base = _corpus(tmp_path)
    gold = _gold(base, [_case(prohibited_conclusion=HER_PROHIBITED_PROSE)])
    run_mod.run(base=base, gold_path=gold)
    assert seen, "the recorder saw no text at all — the injection did not take"
    assert not any("Meridian" in t for t in seen)


def test_an_ungraded_prohibited_target_is_not_evaluated_not_avoided(
    tmp_path: Path,
) -> None:
    """A loaded-but-unscored field must not read as coverage that does not exist.

    The evaluator is what grades this one; the mechanical support check cannot,
    because her prohibition is prose and carries no literal to match.
    """
    base = _corpus(tmp_path)
    gold = _gold(base, [_case(prohibited_conclusion=HER_PROHIBITED_PROSE)])
    result = run_mod.run(base=base, gold_path=gold)
    agg = result.agg
    assert agg.prohibited_verdicts.counts.get(NOT_EVALUATED) == 1
    assert agg.prohibited_verdicts.counts.get(AVOIDED, 0) == 0
    assert agg.prohibited_verdicts.n_graded == 0
    assert agg.prohibited_verdicts.rate is None


def test_the_prohibited_verdict_is_counted_apart_from_the_two_support_targets() -> None:
    """S-1 forbids one merged number, and a negative target is not a third half."""
    evals = [
        QueryEval(
            query_id="q01",
            query="How long is the notification deadline?",
            expected={"adverse-action#notification-timing"},
            unanswerable=False,
            retrieved=[("adverse-action#notification-timing", 0.9)],
            threshold=0.1,
            prohibited_verdict=ASSERTED,
        )
    ]
    agg = aggregate(evals)
    assert agg.prohibited_verdicts.counts == {ASSERTED: 1}
    # The two support targets are untouched by it.
    assert agg.conclusion_verdicts.counts == {NOT_EVALUATED: 1}
    assert agg.summary_verdicts.counts == {NOT_EVALUATED: 1}
    # `avoided` is the numerator here, not `supported`.
    assert agg.prohibited_verdicts.rate == 0.0
    assert PROHIBITED_STATES[0] == AVOIDED
