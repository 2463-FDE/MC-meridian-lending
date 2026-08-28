"""Integration test: full harness run over the real corpus (spec Process 10).

Copies the real `policies/` + `kb_dump/` into a tmp base so the run is
hermetic (no writes to the working tree's cache/report), then asserts the
report carries both required findings (D1.6 data gap, D2.2 refusal) and no
raw PII (Sec/Comp 5).
"""

import json
import shutil
from pathlib import Path

import pytest

from rag_eval.metrics import SUPPORTED
from rag_eval.run import run

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def real_corpus(tmp_path: Path) -> Path:
    shutil.copytree(REPO_ROOT / "policies", tmp_path / "policies")
    shutil.copytree(REPO_ROOT / "kb_dump", tmp_path / "kb_dump")
    return tmp_path


def test_full_run_over_real_corpus(real_corpus: Path):
    result = run(base=real_corpus)
    report = result.report_text

    # D2.2 — kb_dump refused with expected finding profile: SSN+PAN in 5 of 6
    # records, EIN in the 6th.
    kb = next(v for v in result.verdicts if v.path.endswith("applications.jsonl"))
    assert not kb.passed
    counts = kb.counts()
    assert counts["field:ssn"] == 5
    assert counts["field:pan"] == 5
    assert counts["field:ein"] == 1

    # D2.3 — both policy docs pass the gate and get indexed.
    md_verdicts = [v for v in result.verdicts if v.path.endswith(".md")]
    assert len(md_verdicts) == 2 and all(v.passed for v in md_verdicts)
    assert result.n_chunks == 9

    # D1.6 — data-gaps section states the #6012 root cause with citations.
    assert "data-capture failure, not a retrieval bug" in report
    assert "decisions(app_id, outcome)" in report
    assert "db/init/001_schema.sql" in report
    assert "logs/payment-service.log:14" in report
    assert "refer band (600–659)" in report

    # D2.5 — hygiene findings appear alongside retrieval metrics.
    assert "REFUSED" in report and "applications.jsonl" in report
    assert "hit@1" in report and "MRR" in report

    # D1.4/DL-6 — both unanswerable queries evaluated; off-corpus control
    # lands below threshold.
    unans = {e.query_id: e for e in result.evals if e.unanswerable}
    assert set(unans) == {"q11-why-6012-denied", "q12-off-corpus"}
    assert unans["q12-off-corpus"].correct

    # Retrieval sanity on the real corpus: every answerable query finds its
    # expected chunk in the top 5.
    assert all(e.correct for e in result.evals if not e.unanswerable)

    # The by-topic report the client asked for has to survive a real run. The
    # axis shipped with `topic` on no committed case, so every case took the
    # `unmapped` default and the table rendered a single `unmapped` row covering
    # all twelve — present in code, absent from the output she is sent.
    assert "| Topic | Cases | Correct |" in report
    assert "| `credit_decisioning` | 2 |" in report
    assert "| `fee_schedule` | 2 |" in report
    # `unmapped` is reported as a count under the table, never as a topic row.
    assert "| `unmapped` |" not in report
    assert "2 case(s) carry `unmapped`" in report

    # The committed gold set carries two mechanical `support_literal` cases (M-1
    # review finding): the eval must actually grade them, not leave every
    # committed case at `not_evaluated`.
    by_id = {e.query_id: e for e in result.evals}
    assert by_id["q04-adverse-action-timing"].conclusion_verdict == SUPPORTED
    assert by_id["q10-retention"].conclusion_verdict == SUPPORTED
    assert result.agg.conclusion_verdicts.counts.get(SUPPORTED, 0) == 2
    assert result.agg.conclusion_verdicts.n_graded == 2
    assert result.agg.conclusion_verdicts.rate == 1.0

    # The report shows per-case verdicts (M-2) and a graded rate, not raw counts
    # a reader cannot turn into a rate (M-3).
    assert (
        "| Question id | Expected chunk(s) | Top retrieved (score) | hit@1/3/5 | RR | Verdict | Conclusion | Summary |"
        in report
    )
    assert "| q04-adverse-action-timing |" in report and "| supported |" in report
    assert "Graded rate: **1.00** (2 of 2 graded)" in report
    # No support_literal on the displayed-summary axis yet, so it stays n/a
    # rather than reading as a coincidental 0.00.
    assert "Graded rate: **n/a** (0 of 0 graded)" in report


def test_no_raw_pii_in_report_or_cache(real_corpus: Path):
    result = run(base=real_corpus)
    sensitive: set[str] = set()
    for line in (
        (real_corpus / "kb_dump" / "applications.jsonl").read_text().splitlines()
    ):
        if not line.strip():
            continue
        record = json.loads(line)
        for key in ("ssn", "pan", "dob", "ein", "name", "address"):
            if record.get(key):
                sensitive.add(str(record[key]))
    assert sensitive  # the fixture corpus really is contaminated
    cache_text = (real_corpus / "rag_eval" / ".cache" / "embeddings.json").read_text()
    for value in sensitive:
        assert value not in result.report_text
        assert value not in cache_text


def test_second_run_reembeds_nothing_on_real_corpus(real_corpus: Path):
    first = run(base=real_corpus)
    assert first.cache_misses == 9
    second = run(base=real_corpus)
    assert second.cache_misses == 0
    assert "nothing re-embedded" in second.report_text


# Retrieval-quality floors. These are pinned MEASUREMENTS of this corpus under the
# deterministic TF-IDF embedder, not aspirations — same posture tila-vectors-gate takes
# toward its expectations. A value moving in EITHER direction is a deliberate re-pin.
#
# `test_full_run_over_real_corpus` above already floors k=5: `all(e.correct ...)` is
# hit@5 == 1.00, because QueryEval.correct is hits[max(K_VALUES)]. That is the WEAKEST
# of the three. A ranking collapse that pushes every expected chunk from rank 1 to rank 5
# leaves it green while hit@1 falls 0.90 -> 0.00 and MRR falls 0.95 -> 0.20. These are the
# ranking floors it cannot see, and the reason the gate could not tell a healthy index
# from a barely-working one.
_FLOOR_HIT_AT_1 = 0.90
_FLOOR_HIT_AT_3 = 1.00
_FLOOR_MRR = 0.95
_FLOOR_UNANSWERABLE_CORRECT = 1

# The DENOMINATOR is pinned too, not just the rates. A rate floor computed over a gold
# set trimmed to one easy query reports 1.00 and measures nothing — the same "audit the
# list, not only the entries on it" failure `spec_diff_gate.sh` shipped. Growing the gold
# set is expected to fail here: that forces the new query's effect on every floor to be
# looked at once, deliberately, instead of diluting them silently.
_GOLD_ANSWERABLE = 10
_GOLD_UNANSWERABLE = 2


def test_retrieval_quality_floor(real_corpus: Path):
    """The gate fails when ranking quality regresses, not only when retrieval breaks.

    Floors are `>=` against exact literals. The values are deterministic (TF-IDF over a
    fixed corpus), and any float representation edge lands on the failing side of a `>=`,
    which is the safe direction for a gate: a spurious red is investigated, a spurious
    green is not.

    Only `q12-off-corpus` is expected to abstain correctly. `q11-why-6012-denied` retrieves
    a process chunk above threshold and is scored WRONG on purpose (report Data-gaps): it
    is answerable from the decision record, not the corpus, so the floor is 1 of 2 rather
    than 2 of 2. Raising it is the D16/answerability work, not a tuning exercise.
    """
    result = run(base=real_corpus)
    agg = result.agg

    # The floors are calibrated to ONE backend. A swap to Bedrock/Titan measures
    # differently (hit@1 0.90 -> 0.70, MRR 0.95 -> 0.85 on this same corpus), so a silent
    # default change must not be read as a quality regression — or the reverse.
    # The digest after the colon is a corpus hash and moves whenever `policies/` is
    # edited, so only the family is pinned.
    assert result.embedder_signature.startswith("tfidf-v1:")

    assert (agg.n_answerable, agg.n_unanswerable) == (
        _GOLD_ANSWERABLE,
        _GOLD_UNANSWERABLE,
    )
    assert agg.hit_at_k[1] >= _FLOOR_HIT_AT_1
    assert agg.hit_at_k[3] >= _FLOOR_HIT_AT_3
    assert agg.mrr >= _FLOOR_MRR
    assert agg.unanswerable_correct >= _FLOOR_UNANSWERABLE_CORRECT
