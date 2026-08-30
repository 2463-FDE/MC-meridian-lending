"""A manifest-admitted filename must never become an officer-visible chunk id.

Manifest admission exists so a supplied corpus is indexed without renaming the
files it was approved under. It admits on the listed digest, which means the
lowercase-slug rule (`_SAFE_FILENAME`) — the check that keeps an unlabeled person
name out of a chunk id — never runs on that path. `scan_text` does not close the
gap: it is label-gated, so a bare `Jane-Doe.md` carries no self-identifying shape
and passes.

No structural rule separates a person name from a policy code, so the name is not
graded at all here — under a manifest the doc id derives from the approved DIGEST
instead, which is deterministic, non-identifying, and not invertible to the name.
"""

import hashlib
import re
from pathlib import Path

from rag_eval.chunker import chunk_markdown
from rag_eval.run import corpus_doc_id, run

BODY = (
    "# Adverse Action\n\n## Notification timing\n\nNotify within 30 days.\n"
    "\n## Records\n\nRetain for 25 months.\n"
)
PERSON_NAME = "Jane-Doe.md"
DOC_ID = re.compile(r"doc-[0-9a-f]{12}")


def _corpus(tmp_path: Path, names) -> dict[str, str]:
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


def test_manifest_admitted_name_is_not_the_doc_id(tmp_path: Path):
    digests = _corpus(tmp_path, (PERSON_NAME,))
    manifest = {k: v for k, v in digests.items()}

    doc_id = corpus_doc_id(Path("policies") / PERSON_NAME, manifest=manifest)

    assert DOC_ID.fullmatch(doc_id), doc_id
    assert "jane" not in doc_id
    assert "doe" not in doc_id


def test_slug_corpus_keeps_its_stem_without_a_manifest(tmp_path: Path):
    # The committed corpus is admitted by the slug convention, which already
    # grades the name. Readable ids there are the gold set's contract.
    assert corpus_doc_id(Path("policies/fee-schedule.md")) == "fee-schedule"


def test_chunk_ids_carry_no_name_on_the_manifest_path(tmp_path: Path, monkeypatch):
    digests = _corpus(tmp_path, (PERSON_NAME,))
    mf = _manifest(tmp_path, digests)
    doc_id = corpus_doc_id(Path("policies") / PERSON_NAME, manifest=digests)
    (tmp_path / "gold.json").write_text(
        '{"queries": [{"id": "q01-notify", "query": "notification deadline", '
        f'"expected": ["{doc_id}#notification-timing"]}}]}}',
        encoding="utf-8",
    )
    monkeypatch.setattr("rag_eval.run.GOLD_PATH", tmp_path / "gold.json")

    result = run(base=tmp_path, manifest_path=mf)

    assert result.n_chunks == 2
    report = (tmp_path / "rag_eval" / "eval_report.md").read_text(encoding="utf-8")
    assert "Jane" not in report and "jane" not in report


def test_chunker_takes_the_mapped_doc_id(tmp_path: Path):
    path = tmp_path / PERSON_NAME
    path.write_text(BODY, encoding="utf-8")

    chunks = chunk_markdown(path, doc_id="doc-abc123abc123")

    assert [c.chunk_id for c in chunks] == [
        "doc-abc123abc123#notification-timing",
        "doc-abc123abc123#records",
    ]
    assert all(c.doc == "doc-abc123abc123" for c in chunks)
