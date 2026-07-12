"""Unit tests for the runner: threshold calibration + gate enforcement (spec D2.4, DL-6)."""

from pathlib import Path

from rag_eval.run import calibrate_threshold, run


def test_calibrate_picks_minimal_error_split():
    # answerable tops all >= 0.3, unanswerable all <= 0.1: clean separation.
    t = calibrate_threshold([0.5, 0.3, 0.4], [0.1, 0.05])
    assert 0.1 < t < 0.3


def test_calibrate_overlapping_scores_minimizes_errors():
    # One unanswerable (0.35) sits above the weakest answerable (0.25):
    # best split leaves exactly one error, below 0.25.
    t = calibrate_threshold([0.25, 0.5, 0.6], [0.35, 0.05])
    assert 0.05 < t < 0.25


def test_calibrate_degenerate_single_point():
    assert calibrate_threshold([0.5], []) == 0.0
    assert calibrate_threshold([], []) == 0.0


def test_gate_blocks_contaminated_file_from_index(tmp_path: Path):
    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "clean.md").write_text(
        "# Clean Policy\n\n## Fees\n\nLate payment fee is $35 flat.\n",
        encoding="utf-8",
    )
    (policies / "dirty.md").write_text(
        "# Leaky Doc\n\n## Applicant\n\nSSN 123-45-6789 on file.\n",
        encoding="utf-8",
    )

    result = run(base=tmp_path)

    by_path = {Path(v.path).name: v for v in result.verdicts}
    assert by_path["clean.md"].passed
    assert not by_path["dirty.md"].passed
    # Only the clean file's single section was chunked/embedded (D2.4).
    assert result.n_chunks == 1
    # Refused file's content never reaches cache or report in raw form.
    cache_text = (tmp_path / "rag_eval" / ".cache" / "embeddings.json").read_text()
    assert "123-45-6789" not in cache_text
    assert "123-45-6789" not in result.report_text
    # Hygiene verdicts do appear in the report (D2.5), masked.
    assert "dirty.md" in result.report_text
    assert "REFUSED" in result.report_text


def test_empty_corpus_aborts_loudly(tmp_path: Path):
    # Teeth finding: no corpus must not yield a plausible-looking empty report.
    import pytest

    with pytest.raises(RuntimeError, match="no gate-passed corpus"):
        run(base=tmp_path)


def test_all_refused_corpus_aborts_loudly(tmp_path: Path):
    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "dirty.md").write_text(
        "# Leaky\n\n## A\n\nSSN 123-45-6789.\n", encoding="utf-8"
    )
    import pytest

    with pytest.raises(RuntimeError, match="1 refused"):
        run(base=tmp_path)


def test_second_run_hits_cache_entirely(tmp_path: Path):
    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "clean.md").write_text(
        "# Clean Policy\n\n## Fees\n\nLate payment fee is $35 flat.\n",
        encoding="utf-8",
    )
    first = run(base=tmp_path)
    assert first.cache_misses == first.n_chunks
    second = run(base=tmp_path)
    assert second.cache_misses == 0
    assert second.cache_hits == second.n_chunks
