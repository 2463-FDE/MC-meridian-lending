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

import re

import pytest

from rag_eval import report as report_mod
from rag_eval.hygiene import Finding, FileVerdict
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


def _report(
    evals: list[QueryEval],
    n_chunks: int,
    verdicts: list[FileVerdict] | None = None,
    display_names: dict[str, str] | None = None,
) -> str:
    return report_mod.build(
        verdicts=verdicts
        or [FileVerdict(path="policies/fees.md", passed=True, findings=[])],
        n_chunks=n_chunks,
        display_names=display_names,
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
    text = _report(
        [_eval("q1", "What is the late fee?", unanswerable=False, top=0.5)], 66
    )
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
    assert not _looks_like_person_name(f"Does the {phrase} cap the rate on this file?")


def test_false_confidence_note_cites_no_explanation_it_does_not_render() -> None:
    """The note must not point at a per-case explanation the report never writes.

    `_data_gaps_section` renders two fixed subsections and the false-confident
    list; there is no per-case gap rendering, and the two fixed subsections are
    emitted above the note, not below it. A run whose only unanswerable case is
    the origination fee gets a bullet promising an explanation that is not in
    the document.
    """
    ev = _eval("q21", "What is the origination fee?", unanswerable=True, top=0.9)
    text = _report([ev], 66)
    assert "**q21**" in text, "expected a false-confident row for this fixture"
    assert "data-gap sections below" not in text, (
        "the note cross-references sections that are neither below it nor per-case"
    )
    assert "per case" not in text
    assert "no per-case explanation" not in text, (
        "a claim about what the rest of the report contains is the same defect"
    )


def test_denial_gap_section_is_absent_when_no_case_asks_about_it() -> None:
    """The #6012 subsection is a claim about a case, so it needs the case."""
    text = _report(
        [_eval("q21", "What is the origination fee?", unanswerable=True, top=0.9)], 66
    )
    assert "data-capture failure" not in text, (
        "the report explains a denial no case in this run asked about"
    )
    assert "6012" not in text


def test_denial_gap_section_is_kept_when_a_case_asks_about_it() -> None:
    text = _report(
        [_eval("q11", "Why was application #6012 denied?", unanswerable=True, top=0.9)],
        66,
    )
    assert "data-capture failure, not a retrieval bug" in text
    assert "logs/payment-service.log:14" in text


def test_past_applications_gap_needs_a_refusal_in_this_run() -> None:
    """It asserts a hygiene refusal, so it may only appear when one happened."""
    ev = _eval("q1", "What is the late fee?", unanswerable=False, top=0.5)
    clean = _report([ev], 66)
    assert "Past applications contribute nothing" not in clean, (
        "the report asserts a hygiene refusal that this run never made"
    )

    refused = _report(
        [ev],
        66,
        verdicts=[
            FileVerdict(path="kb_dump/applications.jsonl", passed=False, findings=[])
        ],
    )
    assert "Past applications contribute nothing" in refused


def test_denial_gap_section_is_not_emitted_for_the_other_denial() -> None:
    """The subsection is titled and evidenced for #6012, so only #6012 may open it.

    Seed data names two denials with no recorded reason (6012/6013), but the
    section's heading quotes the #6012 question and its only log evidence is
    `app_id=6012`. Opening it for a #6013 question states something about a case
    nobody asked about -- the defect class this file exists to hold shut.
    """
    text = _report(
        [_eval("q13", "Why was application #6013 denied?", unanswerable=True, top=0.9)],
        66,
    )
    assert '#6012 denied?" cannot be answered' not in text, (
        "a #6013 question opened the #6012 subsection"
    )
    assert "app_id=6012" not in text


def _refused_applications(*pii_types: str) -> list[FileVerdict]:
    return [
        FileVerdict(
            path="kb_dump/applications.jsonl",
            passed=False,
            findings=[Finding(t, "\u2022\u2022\u2022\u20220000") for t in pii_types],
        )
    ]


def test_past_applications_gap_states_this_runs_findings() -> None:
    """The parenthetical counts what the gate found, so it must read the verdict.

    "raw SSN/PAN/DOB in five of six records, raw EIN in the sixth" is true of the
    fixture this harness started on and of nothing else. It was emitted whenever a
    refusal for that path existed, whatever the refusal actually found -- the same
    defect as the strings this file already holds shut, one level in.
    """
    ev = _eval("q1", "What is the late fee?", unanswerable=False, top=0.5)
    text = _report([ev], 66, verdicts=_refused_applications("ein"))
    assert "Past applications contribute nothing" in text, (
        "expected the subsection for this fixture"
    )
    assert "five of six records" not in text, (
        "the report states a finding breakdown this run did not produce"
    )
    assert "ein: 1" in text


def test_denial_gap_ignores_a_number_that_merely_contains_the_app_id() -> None:
    """The gate was a substring search over free query text.

    An account number containing the digits, or a dollar amount, opened the whole
    #6012 denial narrative on a run that asked nothing about a denial.
    """
    for query in (
        "What is the balance on account 46012?",
        "Is a $6012.50 loan inside the allowed range?",
    ):
        text = _report([_eval("q30", query, unanswerable=True, top=0.9)], 66)
        assert "data-capture failure" not in text, (
            f"{query!r} opened the #6012 subsection"
        )


def test_denial_gap_opens_on_the_case_id_when_the_question_is_reworded() -> None:
    """The id is the stable key: a reworded question is still the same case."""
    text = _report(
        [
            _eval(
                "q11-why-6012-denied",
                "Why was the second denial refused?",
                unanswerable=True,
                top=0.9,
            )
        ],
        66,
    )
    assert "data-capture failure, not a retrieval bug" in text


def test_denial_gap_needs_an_unanswerable_case() -> None:
    """The subsection asserts the case cannot be answered, so it must be one.

    Once ADR 0008's decision-record fields exist the #6012 case becomes
    answerable, and a gate keyed on the app id alone keeps claiming otherwise.
    """
    text = _report(
        [
            _eval(
                "q11", "Why was application #6012 denied?", unanswerable=False, top=0.9
            )
        ],
        66,
    )
    assert "cannot be answered" not in text, (
        "the report says an answerable case cannot be answered"
    )


def test_past_applications_gap_names_the_file_through_display_names() -> None:
    """A refused file is named through `display_names`, never the raw path.

    `run.py` states the rule where it prints refusals: a manifest-admitted
    filename is graded by nothing and can itself be the identifier. The hygiene
    table obeys it; this subsection hardcoded the path in its prose.
    """
    text = _report(
        [_eval("q1", "What is the late fee?", unanswerable=False, top=0.5)],
        66,
        verdicts=_refused_applications("ein"),
        display_names={"kb_dump/applications.jsonl": "doc-9f3a1c"},
    )
    assert "doc-9f3a1c" in text
    assert "kb_dump/applications.jsonl" not in text, (
        "the subsection prints the raw path the display-name map exists to replace"
    )


@pytest.mark.parametrize("n_chunks", [8, 11, 18, 80])
def test_calibration_note_reads_correctly_for_any_corpus_size(n_chunks: int) -> None:
    """The corpus size is interpolated, so no wording may assume its first digit.

    `a {n}-chunk corpus` renders "a 8-chunk corpus" on the sizes whose spoken
    form starts with a vowel. The report is the graded deliverable and a
    reviewer reads this sentence, so the phrasing must not depend on the number.
    """
    text = _report(
        [_eval("q1", "What is the late fee?", unanswerable=False, top=0.5)], n_chunks
    )
    assert re.search(rf"\b{n_chunks}\b", text), "expected the size in the report"
    assert not re.search(rf"\ba {n_chunks}\b", text), (
        f"the calibration note reads 'a {n_chunks}...' where English wants 'an'"
    )


def test_past_applications_gap_does_not_match_a_similarly_named_directory() -> None:
    """The path gate is a suffix match, so it must anchor on the separator.

    `legacy_kb_dump/applications.jsonl` is a different file; a refusal for it
    opened a subsection whose every claim is about `kb_dump/applications.jsonl`.
    """
    ev = _eval("q1", "What is the late fee?", unanswerable=False, top=0.5)
    lookalike = [
        FileVerdict(
            path="/base/corpus/legacy_kb_dump/applications.jsonl",
            passed=False,
            findings=[Finding("ein", "\u2022\u2022\u2022\u20220000")],
        )
    ]
    assert "Past applications contribute nothing" not in _report(
        [ev], 66, verdicts=lookalike
    ), "a refusal for a different file opened the past-applications subsection"

    real = [
        FileVerdict(
            path="/base/kb_dump/applications.jsonl",
            passed=False,
            findings=[Finding("ein", "\u2022\u2022\u2022\u20220000")],
        )
    ]
    assert "Past applications contribute nothing" in _report([ev], 66, verdicts=real)
