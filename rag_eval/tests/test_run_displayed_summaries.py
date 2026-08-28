"""The synthetic-displayed-summaries package is a second frozen artifact.

It ships outside the policy corpus (S-2, docs/handoffs/2026-08-27-rag-eval-support-test.md)
with its own `shasum -a 256` manifest, sitting next to the files it covers. It must be
audited the same way the policy corpus is — and before ingest or retrieval touch
anything — so a support-test run never scores against a summaries file that drifted
from what she approved.
"""

import hashlib
from pathlib import Path

import pytest

from rag_eval.run import run

BODY = (
    "# Adverse Action\n\n## Notification timing\n\nNotify within 30 days.\n"
    "\n## Records\n\nRetain for 25 months.\n"
)
NAME = "SYN-POL-ADVERSE-ACTION.md"


def _corpus(tmp_path: Path) -> dict[str, str]:
    policies = tmp_path / "policies"
    policies.mkdir(parents=True, exist_ok=True)
    (policies / NAME).write_text(BODY, encoding="utf-8")
    digest = hashlib.sha256((policies / NAME).read_bytes()).hexdigest()
    return {f"policies/{NAME}": digest}


def _manifest(path: Path, entries: dict[str, str]) -> Path:
    mf = path / "SHA256SUMS.txt"
    mf.write_text("".join(f"{d}  {n}\n" for n, d in entries.items()), encoding="utf-8")
    return mf


def _gold(tmp_path: Path, expected: str) -> None:
    (tmp_path / "gold.json").write_text(
        '{"queries": [{"id": "q01-notify", "query": "notification deadline", '
        f'"expected": ["{expected}"]}}]}}',
        encoding="utf-8",
    )


def _summaries_package(
    tmp_path: Path, content: str = "officer,summary\n1,ok\n"
) -> Path:
    pkg = tmp_path / "summaries"
    pkg.mkdir()
    (pkg / "displayed-summaries.csv").write_text(content, encoding="utf-8")
    digest = hashlib.sha256((pkg / "displayed-summaries.csv").read_bytes()).hexdigest()
    _manifest(pkg, {"displayed-summaries.csv": digest})
    return pkg / "SHA256SUMS.txt"


def test_matching_displayed_summaries_do_not_block_the_run(tmp_path: Path, monkeypatch):
    digests = _corpus(tmp_path)
    mf = _manifest(tmp_path, digests)
    _gold(tmp_path, "syn-pol-adverse-action#notification-timing")
    monkeypatch.setattr("rag_eval.run.GOLD_PATH", tmp_path / "gold.json")
    summaries_mf = _summaries_package(tmp_path)

    result = run(
        base=tmp_path,
        manifest_path=mf,
        displayed_summaries_manifest_path=summaries_mf,
    )
    assert result.n_chunks == 2


def test_mutated_displayed_summaries_abort_the_run(tmp_path: Path, monkeypatch):
    digests = _corpus(tmp_path)
    mf = _manifest(tmp_path, digests)
    _gold(tmp_path, "syn-pol-adverse-action#notification-timing")
    monkeypatch.setattr("rag_eval.run.GOLD_PATH", tmp_path / "gold.json")
    summaries_mf = _summaries_package(tmp_path)
    # Drift the package AFTER its manifest was pinned — the same shape as an
    # edited policy file, but on the unrelated artifact.
    (summaries_mf.parent / "displayed-summaries.csv").write_text(
        "officer,summary\n1,edited\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="displayed-summaries"):
        run(
            base=tmp_path,
            manifest_path=mf,
            displayed_summaries_manifest_path=summaries_mf,
        )


def test_displayed_summaries_are_checked_before_ingest(tmp_path: Path, monkeypatch):
    # No corpus at all, and no manifest for it — ingest would fail on its own
    # ("non-slug") if it ran first. The displayed-summaries mismatch must
    # surface instead, proving the audit happens before ingest, not alongside it.
    _gold(tmp_path, "syn-pol-adverse-action#notification-timing")
    monkeypatch.setattr("rag_eval.run.GOLD_PATH", tmp_path / "gold.json")
    summaries_mf = _summaries_package(tmp_path)
    (summaries_mf.parent / "displayed-summaries.csv").write_text(
        "officer,summary\n1,edited\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="displayed-summaries"):
        run(base=tmp_path, displayed_summaries_manifest_path=summaries_mf)


def test_unusable_displayed_summaries_manifest_aborts_the_run(
    tmp_path: Path, monkeypatch
):
    digests = _corpus(tmp_path)
    mf = _manifest(tmp_path, digests)
    _gold(tmp_path, "syn-pol-adverse-action#notification-timing")
    monkeypatch.setattr("rag_eval.run.GOLD_PATH", tmp_path / "gold.json")
    bad_mf = tmp_path / "summaries" / "SHA256SUMS.txt"
    bad_mf.parent.mkdir()
    bad_mf.write_text("not a manifest line\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="displayed-summaries manifest unusable"):
        run(base=tmp_path, manifest_path=mf, displayed_summaries_manifest_path=bad_mf)
