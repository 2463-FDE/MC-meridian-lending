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
    policies.mkdir(parents=True)
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


def _with_title_case_section(base: Path) -> Path:
    """Her real headings are title-case ("Adverse Action"), the fixture's are not."""
    doc = base / "policies" / "syn-pol-fees.md"
    doc.write_text(
        doc.read_text(encoding="utf-8")
        + "\n## Adverse Action\n\nAn adverse action notice goes out within 30 days.\n",
        encoding="utf-8",
    )
    return base


def test_title_case_anchor_heading_is_not_refused_as_a_person_name(
    tmp_path: Path,
) -> None:
    """A structured anchor is not free text: her headings are title-case, and the
    person-name heuristic reads any two title-case words as a probable name."""
    base = _with_title_case_section(_corpus(tmp_path))
    gold = _gold(
        base,
        [
            {
                "id": "q06",
                "query": "When does an adverse action notice go out?",
                "source_document": "syn-pol-fees.md",
                "source_heading": "Adverse Action",
                "outcome_class": "answer",
            }
        ],
    )
    result = run_mod.run(base=base, gold_path=gold)
    assert result.agg.hit_at_k[1] == 1.0


def test_person_name_in_gold_free_text_is_still_refused(tmp_path: Path) -> None:
    """Exempting the anchor must not exempt the query/note it sits beside."""
    base = _with_title_case_section(_corpus(tmp_path))
    gold = _gold(
        base,
        [
            {
                "id": "q06",
                "query": "Did Jane Doe get an adverse action notice?",
                "source_document": "syn-pol-fees.md",
                "source_heading": "Adverse Action",
                "outcome_class": "answer",
            }
        ],
    )
    with pytest.raises(RuntimeError, match="probable person name"):
        run_mod.run(base=base, gold_path=gold)


def test_anchor_keys_are_accepted_in_the_delivered_spelling(tmp_path: Path) -> None:
    """Her rows carry sourceDocument/sourceHeading; both spellings must load."""
    base = _corpus(tmp_path)
    gold = _gold(
        base,
        [
            {
                "id": "q01",
                "query": "What is the late payment fee?",
                "sourceDocument": "syn-pol-fees.md",
                "sourceHeading": "Late fee",
                "outcome_class": "answer",
            }
        ],
    )
    result = run_mod.run(base=base, gold_path=gold)
    assert result.agg.hit_at_k[1] == 1.0


def test_both_anchor_key_spellings_at_once_are_refused(tmp_path: Path) -> None:
    """Silent precedence between two spellings would score against a hidden pick."""
    base = _corpus(tmp_path)
    gold = _gold(
        base,
        [
            {
                "id": "q01",
                "query": "What is the late payment fee?",
                "source_document": "syn-pol-fees.md",
                "sourceDocument": "syn-pol-fees.md",
                "source_heading": "Late fee",
                "sourceHeading": "Origination fee",
                "outcome_class": "answer",
            }
        ],
    )
    with pytest.raises(RuntimeError, match="both spellings"):
        run_mod.run(base=base, gold_path=gold)


_CALIBRATION_BASE = [
    {
        "id": "q01",
        "query": "What is the late payment fee?",
        "source_document": "syn-pol-fees.md",
        "source_heading": "Late fee",
        "outcome_class": "answer",
    },
    {
        "id": "q21",
        "query": "What is the wire transfer fee?",
        "unanswerable": True,
        "outcome_class": "no_match",
    },
]


def test_clarification_does_not_move_the_calibrated_threshold(tmp_path: Path) -> None:
    """A row scored on nothing must not steer the threshold every scored row is
    graded against. "late payment" tops at 0.687, between the abstention top
    (0.487) and the answerable top (0.821), so admitting it as answerable pulls
    the midpoint down from 0.654 to 0.587."""
    base_a = _corpus(tmp_path / "a")
    without = run_mod.run(base=base_a, gold_path=_gold(base_a, _CALIBRATION_BASE))
    base_b = _corpus(tmp_path / "b")
    with_clarification = run_mod.run(
        base=base_b,
        gold_path=_gold(
            base_b,
            _CALIBRATION_BASE
            + [
                {"id": "q13", "query": "late payment", "outcome_class": "clarification"}
            ],
        ),
    )
    assert with_clarification.threshold == without.threshold, (
        "a clarification case entered threshold calibration as an answerable "
        "example, so it moved the threshold every scored case is graded against"
    )


def test_clarification_row_is_not_rendered_as_a_failed_case(tmp_path: Path) -> None:
    """The report says these cases are scored on nothing; the per-question table
    must not then print them as a miss beside real failures."""
    base = _corpus(tmp_path)
    gold = _gold(
        base,
        [ANCHORED, {"id": "q13", "query": "Fees?", "outcome_class": "clarification"}],
    )
    run_mod.run(base=base, gold_path=gold)
    text = (base / "rag_eval" / "eval_report.md").read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| q13 |"))
    assert "✗" not in row, f"clarification row rendered as a scored failure: {row}"
    assert "not scored" in row
