"""A gold set names its anchor by source document and heading, not by chunk id.

Two facts about the client's 28-question set force this.

First, a chunk id is not stable across admission modes. `corpus_doc_id` derives
it from the filename under the naming convention and from the approved digest
under manifest admission, so the SAME corpus yields two disjoint id spaces. A
gold set carrying literal `expected` ids is therefore welded to one mode, and
the graded run uses the other. Her rows carry `sourceDocument` and
`sourceHeading` — the frozen anchors — so the harness derives the id under
whichever mode is active and the same file scores identically in both.

Second, her five `clarification` rows carry no anchor at all, in the CSV and in
the authoritative JSONL alike, because they are ambiguous ACROSS documents by
design ("Ambiguous across Adverse Action, Loan Review, and Credit cutoffs").
They cannot be scored on retrieval rank against a single chunk. They are
excluded from scoring and reported as a count with the reason, exactly as
`unmapped` is for topics: not a row scored beside the others, because the
officer channel has no ask-back path to exercise and scoring them would report
coverage the product does not have.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rag_eval import run as run_mod


def _corpus(tmp_path: Path) -> Path:
    policies = tmp_path / "policies"
    policies.mkdir()
    # Slug-named on purpose: this fixture must be admissible under BOTH modes so
    # the mode-independence assertion below can compare them. The client's own
    # filenames are non-slug, which is why manifest admission is her only option.
    (policies / "syn-pol-fees.md").write_text(
        "# Fees\n\n## Late fee\n\nThe late payment fee is $35 flat.\n"
        "\n## Origination fee\n\nThe origination fee is 3 percent.\n",
        encoding="utf-8",
    )
    return tmp_path


def _manifest(base: Path) -> Path:
    import hashlib

    p = base / "MANIFEST.txt"
    rel = Path("policies/syn-pol-fees.md")
    digest = hashlib.sha256((base / rel).read_bytes()).hexdigest()
    p.write_text(f"{digest}  {rel.as_posix()}\n", encoding="utf-8")
    return p


def _gold(base: Path, queries: list[dict]) -> Path:
    p = base / "gold.json"
    p.write_text(json.dumps({"queries": queries}), encoding="utf-8")
    return p


ANCHORED = {
    "id": "q01",
    "query": "What is the late payment fee?",
    "source_document": "syn-pol-fees.md",
    "source_heading": "Late fee",
    "outcome_class": "answer",
}


def test_anchor_resolves_under_both_admission_modes(tmp_path: Path) -> None:
    """The same gold file must score identically with and without a manifest."""
    base = _corpus(tmp_path)
    gold = _gold(base, [ANCHORED])
    convention = run_mod.run(base=base, gold_path=gold)
    manifest = run_mod.run(base=base, gold_path=gold, manifest_path=_manifest(base))
    assert convention.agg.hit_at_k[1] == 1.0
    assert manifest.agg.hit_at_k[1] == 1.0, (
        "anchor did not resolve under manifest admission — the gold set is "
        "welded to one admission mode"
    )


def test_literal_expected_would_not_survive_the_mode_switch(tmp_path: Path) -> None:
    """Guards the reason anchors exist: literal ids are mode-specific."""
    base = _corpus(tmp_path)
    rel = Path("policies/syn-pol-fees.md")
    man = run_mod.load_corpus_manifest(_manifest(base))
    assert run_mod.corpus_doc_id(rel, None) != run_mod.corpus_doc_id(rel, man)


def test_clarification_without_an_anchor_loads_and_is_not_scored(
    tmp_path: Path,
) -> None:
    """Her clarification rows have no anchor by design; they must not be refused."""
    base = _corpus(tmp_path)
    gold = _gold(
        base,
        [
            ANCHORED,
            {
                "id": "q13",
                "query": "What is our policy on fees?",
                "outcome_class": "clarification",
            },
        ],
    )
    result = run_mod.run(base=base, gold_path=gold)
    assert result.agg.n_unscorable == 1
    assert result.agg.n_answerable == 1, (
        "a clarification case was counted in the answerable denominator"
    )


def test_clarification_is_not_folded_into_the_abstention_class(
    tmp_path: Path,
) -> None:
    """Folding it into no_match would corrupt the one ratio that proves abstention."""
    base = _corpus(tmp_path)
    gold = _gold(
        base,
        [
            ANCHORED,
            {"id": "q13", "query": "Fees?", "outcome_class": "clarification"},
            {
                "id": "q21",
                "query": "What is the wire transfer fee?",
                "unanswerable": True,
                "outcome_class": "no_match",
            },
        ],
    )
    result = run_mod.run(base=base, gold_path=gold)
    assert result.agg.n_unanswerable == 1, "clarification leaked into no_match"
    assert result.agg.n_unscorable == 1


def test_report_states_the_unscorable_count_and_reason(tmp_path: Path) -> None:
    base = _corpus(tmp_path)
    gold = _gold(
        base,
        [ANCHORED, {"id": "q13", "query": "Fees?", "outcome_class": "clarification"}],
    )
    run_mod.run(base=base, gold_path=gold)
    text = (base / "rag_eval" / "eval_report.md").read_text(encoding="utf-8")
    assert "1 case(s)" in text and "clarification" in text
    assert "| `clarification` |" not in text, (
        "clarification appears as a scored row beside real classes"
    )
