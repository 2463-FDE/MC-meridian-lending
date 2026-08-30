"""Wiring the offline evaluator (ADR 0022) into the run.

The evaluator merged with nothing calling it, so every summary and prohibited
verdict reported `not_evaluated` however the run was configured. These tests
cover the call site: resolving the displayed summary an officer actually sees,
choosing a backend, and what the harness does with the three verdicts that come
back -- including the two places S-7 gives no second chance.
"""

import hashlib
import json
from pathlib import Path

import pytest

from rag_eval.evaluator import CaseVerdicts
from rag_eval.metrics import (
    ASSERTED,
    AVOIDED,
    HUMAN_REVIEW,
    NOT_EVALUATED,
    RATIONALE_MAX_CHARS,
    SUPPORTED,
    UNSUPPORTED,
)
from rag_eval.run import (
    load_displayed_summaries,
    make_evaluator,
    require_displayed_summaries,
    run,
)

BODY = (
    "# Adverse Action\n\n## Notification timing\n\nNotify within 30 days.\n"
    "\n## Records\n\nRetain for 25 months.\n"
)
NAME = "SYN-POL-ADVERSE-ACTION.md"


def _manifest(path: Path, entries: dict[str, str]) -> Path:
    mf = path / "SHA256SUMS.txt"
    mf.write_text("".join(f"{d}  {n}\n" for n, d in entries.items()), encoding="utf-8")
    return mf


def _corpus(tmp_path: Path) -> Path:
    policies = tmp_path / "policies"
    policies.mkdir(parents=True, exist_ok=True)
    (policies / NAME).write_text(BODY, encoding="utf-8")
    digest = hashlib.sha256((policies / NAME).read_bytes()).hexdigest()
    return _manifest(tmp_path, {f"policies/{NAME}": digest})


def _summaries(tmp_path: Path, content: str) -> Path:
    pkg = tmp_path / "summaries"
    pkg.mkdir(exist_ok=True)
    csv_path = pkg / "displayed-summaries.csv"
    csv_path.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    return _manifest(pkg, {"displayed-summaries.csv": digest})


def _gold(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "gold.json"
    path.write_text(json.dumps({"queries": rows}), encoding="utf-8")
    return path


ANCHOR = "syn-pol-adverse-action#notification-timing"


def _row(qid: str, **extra) -> dict:
    row = {
        "id": qid,
        "query": "notification deadline",
        "expected": [ANCHOR],
        "topic": "adverse_action",
        "expected_conclusion": "Notify within 30 days.",
        "displayed_summary_id": f"{qid.upper()}-ACCEPTABLE-CONCLUSION",
        "prohibited_conclusion": "There is no deadline.",
    }
    row.update(extra)
    return row


class FakeEvaluator:
    """Records what it was asked and returns a fixed set of verdicts."""

    def __init__(self, verdicts: CaseVerdicts | None = None):
        self.verdicts = verdicts or CaseVerdicts(
            conclusion=UNSUPPORTED,
            summary=SUPPORTED,
            prohibited=AVOIDED,
            rationale="because.",
        )
        self.cases = []

    def grade(self, case):
        self.cases.append(case)
        return self.verdicts


def _run(tmp_path, monkeypatch, gold_rows, evaluator=None, summaries_csv=None):
    mf = _corpus(tmp_path)
    smf = _summaries(
        tmp_path,
        summaries_csv
        if summaries_csv is not None
        else "expected_conclusion_id,synthetic_displayed_summary\nQ01-ACCEPTABLE-CONCLUSION,The officer sees this.\n",
    )
    monkeypatch.setattr("rag_eval.run.make_evaluator", lambda: evaluator)
    return run(
        base=tmp_path,
        gold_path=_gold(tmp_path, gold_rows),
        manifest_path=mf,
        displayed_summaries_manifest_path=smf,
    )


# --- load_displayed_summaries -------------------------------------------------


def test_ids_join_case_insensitively(tmp_path: Path):
    """Her package writes `Q01`; the gold set writes `q01`.

    A case-sensitive join returns no summary for any row, and every summary
    verdict comes back `human_review` on a pass S-7 forbids re-running. This is
    the failure the loader exists to prevent, so it is asserted directly.
    """
    mf = _summaries(
        tmp_path,
        "expected_conclusion_id,synthetic_displayed_summary\nQ01-ACCEPTABLE-CONCLUSION,The text.\n",
    )
    assert load_displayed_summaries(mf) == {"q01-acceptable-conclusion": "The text."}


def test_whitespace_in_a_summary_is_collapsed(tmp_path: Path):
    mf = _summaries(
        tmp_path,
        'expected_conclusion_id,synthetic_displayed_summary\nQ01-ACCEPTABLE-CONCLUSION,"a\n  b"\n',
    )
    assert load_displayed_summaries(mf) == {"q01-acceptable-conclusion": "a b"}


@pytest.mark.parametrize(
    "content,expected",
    [
        ("officer,summary\n1,ok\n", "missing column"),
        ("expected_conclusion_id,synthetic_displayed_summary\n", "no rows"),
        (
            "expected_conclusion_id,synthetic_displayed_summary\nQ01-ACCEPTABLE-CONCLUSION,a\nq01-acceptable-conclusion,b\n",
            "twice",
        ),
        (
            "expected_conclusion_id,synthetic_displayed_summary\nQ01-ACCEPTABLE-CONCLUSION,\n",
            "empty summary",
        ),
        ("expected_conclusion_id,synthetic_displayed_summary\n ,a\n", "blank id"),
    ],
)
def test_the_loader_fails_closed(tmp_path: Path, content: str, expected: str):
    """Each of these silently yields an empty target the model would then grade."""
    mf = _summaries(tmp_path, content)
    with pytest.raises(RuntimeError, match=expected):
        load_displayed_summaries(mf)


def test_a_missing_csv_is_refused(tmp_path: Path):
    mf = _summaries(
        tmp_path,
        "expected_conclusion_id,synthetic_displayed_summary\nQ01-ACCEPTABLE-CONCLUSION,a\n",
    )
    (mf.parent / "displayed-summaries.csv").unlink()
    with pytest.raises(RuntimeError, match="unreadable"):
        load_displayed_summaries(mf)


# --- make_evaluator -----------------------------------------------------------


def test_no_judge_by_default(monkeypatch):
    """A graded pass is asked for, never fallen into."""
    monkeypatch.delenv("RAG_JUDGE", raising=False)
    assert make_evaluator() is None


def test_blank_counts_as_unset(monkeypatch):
    monkeypatch.setenv("RAG_JUDGE", "  ")
    assert make_evaluator() is None


def test_an_unknown_backend_fails_loud(monkeypatch):
    monkeypatch.setenv("RAG_JUDGE", "openai")
    with pytest.raises(ValueError, match="not one of"):
        make_evaluator()


def test_bedrock_needs_an_explicit_region(monkeypatch):
    monkeypatch.setenv("RAG_JUDGE", "bedrock")
    monkeypatch.setenv("AWS_REGION", "")
    with pytest.raises(ValueError, match="AWS_REGION"):
        make_evaluator()


def test_the_judge_model_is_overridable(monkeypatch):
    monkeypatch.setenv("RAG_JUDGE", "bedrock")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("RAG_JUDGE_MODEL", "some.other.model-v1:0")
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.setattr(
        "rag_eval.evaluator.BedrockEvaluator.__init__",
        lambda self, *, region, model_id=None, client=None: setattr(
            self, "model_id", model_id
        ),
    )
    assert make_evaluator().model_id == "some.other.model-v1:0"


# --- the grading loop ---------------------------------------------------------


def test_without_a_judge_every_graded_axis_stays_unevaluated(tmp_path, monkeypatch):
    result = _run(tmp_path, monkeypatch, [_row("q01")], evaluator=None)
    e = result.evals[0]
    assert e.summary_verdict == NOT_EVALUATED
    assert e.prohibited_verdict == NOT_EVALUATED
    assert e.rationale == ""


def test_the_verdicts_reach_the_report_row(tmp_path, monkeypatch):
    fake = FakeEvaluator()
    result = _run(tmp_path, monkeypatch, [_row("q01")], evaluator=fake)
    e = result.evals[0]
    assert (e.conclusion_verdict, e.summary_verdict, e.prohibited_verdict) == (
        UNSUPPORTED,
        SUPPORTED,
        AVOIDED,
    )
    assert e.rationale == "because."


def test_the_judge_is_given_the_summary_an_officer_sees(tmp_path, monkeypatch):
    fake = FakeEvaluator()
    _run(tmp_path, monkeypatch, [_row("q01")], evaluator=fake)
    assert fake.cases[0].displayed_summary == "The officer sees this."


def test_the_judge_is_given_the_passages_the_run_retrieved(tmp_path, monkeypatch):
    """Not the expected passage: a conclusion true of a chunk never surfaced is
    not supported BY THE RUN, which is what the support test asks."""
    fake = FakeEvaluator()
    _run(tmp_path, monkeypatch, [_row("q01")], evaluator=fake)
    assert any("30 days" in p for p in fake.cases[0].passages)


def test_a_literal_row_keeps_its_mechanical_conclusion(tmp_path, monkeypatch):
    """S-6, read as the conclusion axis only (ADR 0022).

    The literal decides the conclusion, so the model's answer for THAT axis is
    discarded -- but the row is still graded on the two axes no literal covers,
    which is why it still reaches the model at all.
    """
    fake = FakeEvaluator()
    result = _run(
        tmp_path,
        monkeypatch,
        [_row("q01", support_literal="30 days")],
        evaluator=fake,
    )
    e = result.evals[0]
    assert e.conclusion_verdict == SUPPORTED  # mechanical, not the fake's UNSUPPORTED
    assert e.summary_verdict == SUPPORTED
    assert e.prohibited_verdict == AVOIDED
    assert len(fake.cases) == 1


def test_a_row_without_a_literal_takes_the_model_conclusion(tmp_path, monkeypatch):
    fake = FakeEvaluator()
    result = _run(tmp_path, monkeypatch, [_row("q01")], evaluator=fake)
    assert result.evals[0].conclusion_verdict == UNSUPPORTED


def test_a_row_with_no_support_targets_is_never_sent_to_the_model(
    tmp_path, monkeypatch
):
    """A legacy/retrieval-only row carries no support-test field at all.

    n_paired == 0 is a permitted gold set (rag_eval/run.py's n_paired check),
    so a judged run can still see a row with none of expected_conclusion,
    displayed_summary_id or prohibited_conclusion. Calling the evaluator
    anyway would grade summary/prohibited against an empty-string target and
    report a real verdict for an axis that was never asked about -- the
    evaluator must not be called at all, and every axis must read
    not_evaluated.
    """
    fake = FakeEvaluator()
    legacy_row = {
        "id": "q01",
        "query": "notification deadline",
        "expected": [ANCHOR],
        "topic": "adverse_action",
    }
    result = _run(tmp_path, monkeypatch, [legacy_row], evaluator=fake)
    e = result.evals[0]
    assert e.conclusion_verdict == NOT_EVALUATED
    assert e.summary_verdict == NOT_EVALUATED
    assert e.prohibited_verdict == NOT_EVALUATED
    assert e.rationale == ""
    assert fake.cases == [], "evaluator.grade() must not be called for a targetless row"


def test_a_row_with_only_a_prohibited_target_grades_only_that_axis(
    tmp_path, monkeypatch
):
    """prohibited_conclusion is independent of the expected/displayed_summary_id pair.

    A row can carry a prohibited target with no support pair at all -- the
    call is still needed for the prohibited axis, but conclusion and summary
    must not pick up the model's answer for axes that had no target.
    """
    fake = FakeEvaluator(
        CaseVerdicts(
            conclusion=SUPPORTED,
            summary=SUPPORTED,
            prohibited=ASSERTED,
            rationale="because.",
        )
    )
    row = {
        "id": "q01",
        "query": "notification deadline",
        "expected": [ANCHOR],
        "topic": "adverse_action",
        "prohibited_conclusion": "There is no deadline.",
    }
    result = _run(tmp_path, monkeypatch, [row], evaluator=fake)
    e = result.evals[0]
    assert e.conclusion_verdict == NOT_EVALUATED
    assert e.summary_verdict == NOT_EVALUATED
    assert e.prohibited_verdict == ASSERTED  # the only axis this row asked about
    assert len(fake.cases) == 1


def test_an_over_long_rationale_is_never_persisted(tmp_path, monkeypatch):
    """QueryEval refuses an over-long rationale (S-10).

    Truncating at the call site would still write the first
    RATIONALE_MAX_CHARS of the model's text into the report -- exactly the
    passage-leak S-10 exists to stop, since the harness cannot tell a
    retrieved passage from ordinary prose. The call site must not pass any of
    the over-long text through: it downgrades to human_review with a fixed,
    content-free rationale instead.
    """
    fake = FakeEvaluator(
        CaseVerdicts(
            conclusion=SUPPORTED,
            summary=SUPPORTED,
            prohibited=AVOIDED,
            rationale="x" * (RATIONALE_MAX_CHARS + 50),
        )
    )
    result = _run(tmp_path, monkeypatch, [_row("q01")], evaluator=fake)
    e = result.evals[0]
    assert e.rationale == "evaluator rationale exceeded retention limit"
    assert e.conclusion_verdict == HUMAN_REVIEW  # no literal -> no mechanical verdict
    assert e.summary_verdict == HUMAN_REVIEW
    assert e.prohibited_verdict == HUMAN_REVIEW
    assert "x" * 10 not in e.rationale


def test_an_over_long_rationale_keeps_a_literal_row_mechanical(tmp_path, monkeypatch):
    """A literal-decided conclusion does not depend on the model's rationale.

    S-6: the mechanical check already decided this axis before the model was
    ever called, so an over-long rationale on the same case must not overwrite
    it with human_review.
    """
    fake = FakeEvaluator(
        CaseVerdicts(
            conclusion=UNSUPPORTED,
            summary=SUPPORTED,
            prohibited=AVOIDED,
            rationale="x" * (RATIONALE_MAX_CHARS + 50),
        )
    )
    result = _run(
        tmp_path,
        monkeypatch,
        [_row("q01", support_literal="30 days")],
        evaluator=fake,
    )
    e = result.evals[0]
    assert e.conclusion_verdict == SUPPORTED  # mechanical, untouched
    assert e.summary_verdict == HUMAN_REVIEW
    assert e.rationale == "evaluator rationale exceeded retention limit"


def test_a_human_review_verdict_survives_the_wiring(tmp_path, monkeypatch):
    """S-9: it counts in neither direction, and must not be coerced to a grade."""
    fake = FakeEvaluator(
        CaseVerdicts(
            conclusion=HUMAN_REVIEW,
            summary=HUMAN_REVIEW,
            prohibited=HUMAN_REVIEW,
            rationale="",
        )
    )
    result = _run(tmp_path, monkeypatch, [_row("q01")], evaluator=fake)
    e = result.evals[0]
    assert e.summary_verdict == HUMAN_REVIEW
    assert e.prohibited_verdict == HUMAN_REVIEW


def test_the_unevaluated_note_does_not_claim_the_evaluator_is_unbuilt(
    tmp_path, monkeypatch
):
    """The note ships to the client, and it said "the evaluator is not built".

    It is built and merged (ADR 0022); what varies is whether it ran on this
    pass. The sentence still has to render for a retrieval-only run, so it is
    reworded rather than removed.
    """
    result = _run(tmp_path, monkeypatch, [_row("q01")], evaluator=None)
    body = result.report_text
    assert "the evaluator is not built" not in body
    assert "the evaluator did not run on this pass" in body


# --- the join key, and the coverage check that makes a miss loud ---------------


def test_the_package_is_keyed_on_the_conclusion_id_not_the_question_id(tmp_path: Path):
    """Her package carries BOTH ids, and only one of them is the join key.

    `question_id` holds `Q01`; `expected_conclusion_id` holds
    `Q01-ACCEPTABLE-CONCLUSION`, which is what the gold set's
    `displayed_summary_id` actually contains. Keying on `question_id` resolves
    nothing and raises nothing, so the evaluator is handed 28 empty summaries and
    grades them -- on the one pass S-7 does not allow us to repeat.
    """
    mf = _summaries(
        tmp_path,
        "question_id,expected_conclusion_id,synthetic_displayed_summary\n"
        "Q01,Q01-ACCEPTABLE-CONCLUSION,The text.\n",
    )
    assert load_displayed_summaries(mf) == {"q01-acceptable-conclusion": "The text."}


def test_a_declared_id_that_resolves_to_nothing_stops_the_run():
    with pytest.raises(RuntimeError, match="q02"):
        require_displayed_summaries(
            [_row("q01"), _row("q02")],
            {"q01-acceptable-conclusion": "present"},
        )


def test_a_row_declaring_no_summary_id_is_not_an_error():
    """Its summary verdict stays `not_evaluated` -- neither a pass nor a failure."""
    require_displayed_summaries([_row("q01", displayed_summary_id=None)], {})


def test_coverage_is_checked_before_the_embedder_is_built(tmp_path, monkeypatch):
    """On a provider run the embeds are billed, and S-7 gives one attempt at the
    whole run -- so an unusable summaries package must stop it before that."""
    built = []
    monkeypatch.setattr(
        "rag_eval.run.make_embedder", lambda: built.append(1) or pytest.fail("built")
    )
    with pytest.raises(RuntimeError, match="no displayed summary"):
        _run(
            tmp_path,
            monkeypatch,
            [_row("q01")],
            summaries_csv=(
                "expected_conclusion_id,synthetic_displayed_summary\n"
                "Q99-ACCEPTABLE-CONCLUSION,unrelated.\n"
            ),
        )
    assert built == []


def test_a_declared_summary_id_with_no_package_at_all_is_an_error():
    """`--displayed-summaries-manifest` omitted entirely, not just missing this
    id -- `displayed_summaries` is `{}`, and that must be treated the same as
    "declared but unresolved", not as "no row needs one". The early-return
    version of `require_displayed_summaries` let this row through silently,
    reaching `_graded()` with `displayed_summary=""` -- a judged run grading
    the summary axis against an empty target for every row, the exact
    silent-false-complete this coverage check exists to kill.
    """
    with pytest.raises(RuntimeError, match="q01"):
        require_displayed_summaries([_row("q01")], {})


def test_a_judged_run_with_no_summaries_manifest_at_all_is_an_error(
    tmp_path, monkeypatch
):
    """Same failure at the `run()` boundary: a judge is configured (a real
    RAG_JUDGE=bedrock pass) and the operator forgot
    --displayed-summaries-manifest entirely, not just gave the wrong id. Must
    stop before the embedder is built (S-7: a provider run's embeds are
    billed calls).

    A retrieval-only run with no judge configured at all must NOT trip this
    -- see test_conclusion_fields.py's pre-support-test fixtures, which
    declare displayed_summary_id but never supply a package or a judge.
    """
    built = []
    monkeypatch.setattr("rag_eval.run.make_evaluator", lambda: FakeEvaluator())
    monkeypatch.setattr(
        "rag_eval.run.make_embedder", lambda: built.append(1) or pytest.fail("built")
    )
    mf = _corpus(tmp_path)
    with pytest.raises(RuntimeError, match="q01"):
        run(
            base=tmp_path,
            gold_path=_gold(tmp_path, [_row("q01")]),
            manifest_path=mf,
        )
    assert built == []


def test_a_targetless_gold_set_does_not_claim_a_graded_pass(tmp_path, monkeypatch):
    """RAG_JUDGE=bedrock but every gold row is legacy/retrieval-only: the
    evaluator exists but `_graded()` never calls it, so `judge_calls` stays 0.
    `judge_backend` must not say "bedrock" for a pass that made zero calls --
    the report would then read "graded by bedrock, 0 LLM call(s)", which
    describes a completed judged pass over rows nobody sent to the model.
    """
    fake = FakeEvaluator()
    legacy_row = {
        "id": "q01",
        "query": "notification deadline",
        "expected": [ANCHOR],
        "topic": "adverse_action",
    }
    result = _run(tmp_path, monkeypatch, [legacy_row], evaluator=fake)
    assert fake.cases == []
    assert result.judge_calls == 0
    assert result.judge_backend == "none"
