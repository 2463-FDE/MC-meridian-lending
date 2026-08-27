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


# --- The audited set and the indexed set must be the same set -------------
# `audit_corpus_against_manifest` walks the corpus recursively (rag_eval/run.py),
# and so does the harness's own discovery, so a manifest-approved file in a
# subdirectory audits clean. Indexing it flatly would then drop it silently:
# retrieval abstains on content the manifest explicitly admitted, and nothing
# reports a refusal.

NESTED = "nested/SYN-POL-DEEP.md"
NESTED_BODY = "# Deep Policy\n\n## Escalation\n\nEscalate to a supervisor.\n"


def _nested_corpus(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    base, digests = _corpus(tmp_path)
    deep = base / NESTED
    deep.parent.mkdir(parents=True, exist_ok=True)
    deep.write_text(NESTED_BODY, encoding="utf-8")
    digests[NESTED] = hashlib.sha256(deep.read_bytes()).hexdigest()
    return base, digests


def test_manifest_approved_nested_document_is_indexed(tmp_path: Path, monkeypatch):
    base, digests = _nested_corpus(tmp_path)
    _configure(monkeypatch, base, _manifest(tmp_path, digests))

    ids = {c.chunk_id for c in policy_retrieval._load_corpus()}
    assert any(cid.startswith("syn-pol-deep#") for cid in ids)


def test_manifest_approved_nested_document_is_retrievable(
    tmp_path: Path, monkeypatch
):
    base, digests = _nested_corpus(tmp_path)
    _configure(monkeypatch, base, _manifest(tmp_path, digests))
    monkeypatch.setattr(config, "POLICY_RETRIEVAL_MIN_SCORE", "0.01")

    answer = policy_retrieval.search("escalate to a supervisor")
    assert answer.status == "policy_hit"
    assert "supervisor" in answer.text.lower()


# --- Duplicate chunk ids must abort the index -----------------------------
# The chunker lowercases the filename stem, so two manifest-approved files
# differing only by case emit identical chunk ids. `InMemoryIndex` keeps both
# entries while the id->text map keeps one, so search can score one chunk's
# vector and hand back the other chunk's text — the wrong policy under a
# legitimate-looking citation. The harness aborts on this (rag_eval/run.py);
# the service abstains, which is its fail-closed equivalent.

SAME_STEM = "nested/SYN-POL-ADVERSE-ACTION.md"
CASE_TWIN = "syn-pol-adverse-action.md"
TWIN_BODY = "# Adverse Action\n\n## Notification timing\n\nNotify within 7 days.\n"


def _twin_corpus(tmp_path: Path, twin_name: str) -> tuple[Path, dict[str, str]]:
    base, digests = _corpus(tmp_path)
    twin = base / twin_name
    twin.parent.mkdir(parents=True, exist_ok=True)
    twin.write_text(TWIN_BODY, encoding="utf-8")
    digests[twin_name] = hashlib.sha256(twin.read_bytes()).hexdigest()
    return base, digests


def test_same_stem_in_two_directories_indexes_nothing(tmp_path: Path, monkeypatch):
    # Recursive discovery is what makes this reachable: two approved documents
    # in different folders share a stem, so they share every chunk id.
    base, digests = _twin_corpus(tmp_path, SAME_STEM)
    _configure(monkeypatch, base, _manifest(tmp_path, digests))

    chunks = policy_retrieval._load_corpus()
    # Both files are individually admitted, so the collision is not a refusal.
    assert len(chunks) > len({c.chunk_id for c in chunks})
    assert policy_retrieval._build_index() is None


def test_case_variant_pair_indexes_nothing(tmp_path: Path, monkeypatch):
    base, digests = _twin_corpus(tmp_path, CASE_TWIN)
    if len(list(base.glob("*.md"))) < 2:
        import pytest

        pytest.skip("case-insensitive filesystem cannot hold both spellings")
    _configure(monkeypatch, base, _manifest(tmp_path, digests))

    chunks = policy_retrieval._load_corpus()
    assert len(chunks) > len({c.chunk_id for c in chunks})
    assert policy_retrieval._build_index() is None


def test_no_manifest_does_not_descend_into_subdirectories(
    tmp_path: Path, monkeypatch
):
    # The recursive walk exists to match what the manifest audit grades. Without
    # a manifest nothing is audited, and the corpus arrives over an operator's
    # bind mount — so a draft or an archived copy parked in a subdirectory must
    # not become policy text an officer is shown, admitted by its filename alone.
    base = tmp_path / "policies"
    (base / "sub").mkdir(parents=True)
    (base / "fee_schedule.md").write_text(BODY, encoding="utf-8")
    (base / "sub" / "internal_draft.md").write_text(
        "# Draft\n\n## Pricing\n\nUnapproved draft pricing.\n", encoding="utf-8"
    )
    _configure(monkeypatch, base, None)

    ids = {c.chunk_id for c in policy_retrieval._load_corpus()}
    assert any(cid.startswith("fee_schedule#") for cid in ids)
    assert not any(cid.startswith("internal_draft#") for cid in ids)
