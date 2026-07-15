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


def test_no_raw_pii_in_report_or_cache(real_corpus: Path):
    result = run(base=real_corpus)
    sensitive: set[str] = set()
    for line in (real_corpus / "kb_dump" / "applications.jsonl").read_text().splitlines():
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
