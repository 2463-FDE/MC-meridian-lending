"""The graded run must ingest a supplied corpus, not refuse it.

`run()` has its own corpus scan, separate from the service's. Wiring manifest
admission into `policy_retrieval` alone left the harness — the thing the client
actually authorized — refusing every supplied document and aborting on an empty
corpus.

Manifest keys here are BASE-relative (`policies/X.md`), because `run()` scans
`base/policies` and `base/kb_dump` and reports paths relative to `base`. That is
deliberately different from the service, whose keys are corpus-directory-relative,
and it means a supplied checksum file covering a whole delivery is usable verbatim.
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


def _corpus(tmp_path: Path, names=(NAME,)) -> dict[str, str]:
    policies = tmp_path / "policies"
    policies.mkdir(parents=True, exist_ok=True)
    digests = {}
    for n in names:
        (policies / n).write_text(BODY, encoding="utf-8")
        digests[f"policies/{n}"] = hashlib.sha256(
            (policies / n).read_bytes()
        ).hexdigest()
    return digests


def _manifest(tmp_path: Path, entries: dict[str, str]) -> Path:
    mf = tmp_path / "SHA256SUMS.txt"
    mf.write_text("".join(f"{d}  {n}\n" for n, d in entries.items()), encoding="utf-8")
    return mf


def _gold(tmp_path: Path, expected: str) -> None:
    # run() reads the packaged gold set; point it at a minimal one whose expected
    # chunk id exists in this corpus, so retrieval has something to score.
    (tmp_path / "gold.json").write_text(
        '{"queries": [{"id": "q01-notify", "query": "notification deadline", '
        f'"expected": ["{expected}"]}}]}}',
        encoding="utf-8",
    )


def test_supplied_corpus_is_ingested_with_a_manifest(tmp_path: Path, monkeypatch):
    digests = _corpus(tmp_path)
    mf = _manifest(tmp_path, digests)
    _gold(tmp_path, "syn-pol-adverse-action#notification-timing")
    monkeypatch.setattr("rag_eval.run.GOLD_PATH", tmp_path / "gold.json")

    result = run(base=tmp_path, manifest_path=mf)
    # Two H2 sections; nothing precedes the first heading, so there is no _intro.
    assert result.n_chunks == 2
    assert (tmp_path / "rag_eval" / "eval_report.md").exists()


def test_supplied_corpus_is_refused_without_a_manifest(tmp_path: Path, monkeypatch):
    _corpus(tmp_path)
    _gold(tmp_path, "syn-pol-adverse-action#notification-timing")
    monkeypatch.setattr("rag_eval.run.GOLD_PATH", tmp_path / "gold.json")

    with pytest.raises(RuntimeError, match="non-slug"):
        run(base=tmp_path)


def test_unlisted_policy_file_aborts_the_run(tmp_path: Path, monkeypatch):
    # A file the manifest does not list means the corpus is not the approved one.
    # The run aborts rather than indexing the listed subset.
    digests = _corpus(tmp_path)
    (tmp_path / "policies" / "SYN-POL-UNAPPROVED.md").write_text(
        BODY, encoding="utf-8"
    )
    mf = _manifest(tmp_path, digests)
    _gold(tmp_path, "syn-pol-adverse-action#notification-timing")
    monkeypatch.setattr("rag_eval.run.GOLD_PATH", tmp_path / "gold.json")

    with pytest.raises(RuntimeError, match="manifest"):
        run(base=tmp_path, manifest_path=mf)


def test_mutated_content_aborts_the_run(tmp_path: Path, monkeypatch):
    digests = _corpus(tmp_path)
    mf = _manifest(tmp_path, digests)
    (tmp_path / "policies" / NAME).write_text(BODY + "\nEdited.\n", encoding="utf-8")
    _gold(tmp_path, "syn-pol-adverse-action#notification-timing")
    monkeypatch.setattr("rag_eval.run.GOLD_PATH", tmp_path / "gold.json")

    with pytest.raises(RuntimeError, match="manifest"):
        run(base=tmp_path, manifest_path=mf)


def test_manifest_does_not_govern_the_legacy_dump(tmp_path: Path, monkeypatch):
    # kb_dump is the one corpus root with its own pinned exception. A policy
    # manifest declares the approved POLICY corpus, so it must not turn the legacy
    # dump into an unlisted-file abort — that would conflate two separate controls.
    digests = _corpus(tmp_path)
    dump = tmp_path / "kb_dump"
    dump.mkdir()
    (dump / "applications.jsonl").write_text('{"note": "clean"}\n', encoding="utf-8")
    mf = _manifest(tmp_path, digests)
    _gold(tmp_path, "syn-pol-adverse-action#notification-timing")
    monkeypatch.setattr("rag_eval.run.GOLD_PATH", tmp_path / "gold.json")

    result = run(base=tmp_path, manifest_path=mf)
    assert result.n_chunks == 2


def test_a_whole_delivery_manifest_is_usable_verbatim(tmp_path: Path, monkeypatch):
    # A supplied checksum file covers the whole package. Only its policies/
    # entries are relevant here, and the entries for directories not copied into
    # the working tree must not read as missing files — otherwise the delivery's
    # own manifest would have to be edited before use, and an edited checksum file
    # is exactly what the digest pinning exists to avoid.
    digests = _corpus(tmp_path)
    whole = dict(digests)
    whole["officer-questions/officer-questions-and-acceptance.jsonl"] = "2" * 64
    whole["sources/source-ledger.csv"] = "3" * 64
    mf = _manifest(tmp_path, whole)
    _gold(tmp_path, "syn-pol-adverse-action#notification-timing")
    monkeypatch.setattr("rag_eval.run.GOLD_PATH", tmp_path / "gold.json")

    result = run(base=tmp_path, manifest_path=mf)
    assert result.n_chunks == 2


def test_manifest_covering_no_policy_file_is_refused(tmp_path: Path, monkeypatch):
    _corpus(tmp_path)
    mf = _manifest(tmp_path, {"sources/source-ledger.csv": "3" * 64})
    _gold(tmp_path, "syn-pol-adverse-action#notification-timing")
    monkeypatch.setattr("rag_eval.run.GOLD_PATH", tmp_path / "gold.json")

    with pytest.raises(RuntimeError, match="no approved files under policies"):
        run(base=tmp_path, manifest_path=mf)
