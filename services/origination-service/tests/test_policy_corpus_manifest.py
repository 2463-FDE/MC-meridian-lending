"""Manifest admission on the product retrieval path.

`rag_eval` gained manifest-based admission so the supplied corpus can be indexed
without renaming files it was approved under. The service reads the same corpus
through `policy_retrieval._load_corpus`, so it needs the same admission — otherwise
the harness indexes five documents and the running service refuses all five and
answers "no policy match" to everything.

Fail-closed throughout: an unreadable or malformed manifest, or a corpus that does
not match it, yields no corpus, which the caller already turns into an abstention.
Fixtures are built here rather than read from the supplied packet, which lives
outside the repository.
"""

import hashlib
from pathlib import Path

from app import config, policy_retrieval

BODY = "# Adverse Action\n\n## Notification timing\n\nNotify within 30 days.\n"
NAME = "SYN-POL-ADVERSE-ACTION.md"


def _corpus(tmp_path: Path, names=(NAME,)) -> tuple[Path, dict[str, str]]:
    base = tmp_path / "policies"
    base.mkdir(parents=True, exist_ok=True)
    digests = {}
    for n in names:
        (base / n).write_text(BODY, encoding="utf-8")
        digests[n] = hashlib.sha256((base / n).read_bytes()).hexdigest()
    return base, digests


def _manifest(tmp_path: Path, entries: dict[str, str]) -> Path:
    mf = tmp_path / "CORPUS-SHA256SUMS.txt"
    mf.write_text(
        "".join(f"{d}  {n}\n" for n, d in entries.items()), encoding="utf-8"
    )
    return mf


def _configure(monkeypatch, base: Path, manifest: Path | None):
    monkeypatch.setattr(config, "POLICY_CORPUS_DIR", str(base))
    monkeypatch.setattr(
        config, "POLICY_CORPUS_MANIFEST", str(manifest) if manifest else ""
    )
    policy_retrieval.reset_index_cache()


def test_uppercase_corpus_is_indexed_when_the_manifest_admits_it(
    tmp_path: Path, monkeypatch
):
    base, digests = _corpus(tmp_path)
    _configure(monkeypatch, base, _manifest(tmp_path, digests))

    chunks = policy_retrieval._load_corpus()
    assert chunks
    assert all(c.chunk_id.startswith("syn-pol-adverse-action#") for c in chunks)


def test_uppercase_corpus_is_refused_without_a_manifest(tmp_path: Path, monkeypatch):
    # The lowercase-slug rule still governs when no manifest is configured, which
    # is what keeps an unlabeled person name out of a chunk id.
    base, _ = _corpus(tmp_path)
    _configure(monkeypatch, base, None)

    assert policy_retrieval._load_corpus() == []


def test_corpus_not_matching_the_manifest_yields_nothing(tmp_path: Path, monkeypatch):
    # An unlisted file in the corpus directory means the corpus on disk is not the
    # corpus that was approved. Indexing the listed subset would silently serve a
    # different corpus than the one named in the approval.
    base, digests = _corpus(tmp_path)
    (base / "SYN-POL-UNAPPROVED.md").write_text(BODY, encoding="utf-8")
    _configure(monkeypatch, base, _manifest(tmp_path, digests))

    assert policy_retrieval._load_corpus() == []


def test_mutated_content_yields_nothing(tmp_path: Path, monkeypatch):
    base, digests = _corpus(tmp_path)
    mf = _manifest(tmp_path, digests)
    (base / NAME).write_text(BODY + "\nEdited after approval.\n", encoding="utf-8")
    _configure(monkeypatch, base, mf)

    assert policy_retrieval._load_corpus() == []


def test_malformed_manifest_yields_nothing(tmp_path: Path, monkeypatch):
    base, _ = _corpus(tmp_path)
    mf = tmp_path / "CORPUS-SHA256SUMS.txt"
    mf.write_text("not-a-digest  " + NAME + "\n", encoding="utf-8")
    _configure(monkeypatch, base, mf)

    assert policy_retrieval._load_corpus() == []


def test_missing_manifest_file_yields_nothing(tmp_path: Path, monkeypatch):
    base, _ = _corpus(tmp_path)
    _configure(monkeypatch, base, tmp_path / "absent.txt")

    assert policy_retrieval._load_corpus() == []


def test_manifest_cannot_admit_a_name_that_is_borrower_data(
    tmp_path: Path, monkeypatch
):
    # scan_text runs first and unconditionally: this is the client's second
    # exclusion fixture, whose body is clean and whose filename is not.
    name = "PERSON-ALPHA-ssn-000-00-0000.md"
    base, digests = _corpus(tmp_path, names=(name,))
    _configure(monkeypatch, base, _manifest(tmp_path, digests))

    assert policy_retrieval._load_corpus() == []


def test_existing_lowercase_corpus_still_loads(tmp_path: Path, monkeypatch):
    # The repository's own corpus has no manifest and must keep working.
    base = tmp_path / "policies"
    base.mkdir(parents=True)
    (base / "fee_schedule.md").write_text(BODY, encoding="utf-8")
    _configure(monkeypatch, base, None)

    assert policy_retrieval._load_corpus()
