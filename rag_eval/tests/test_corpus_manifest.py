"""Admission of an approved corpus whose filenames are not lowercase slugs.

The client-supplied training corpus ships as SYN-POL-*.md with a checksum manifest,
and its checksums must stay unchanged — so the files cannot be renamed to satisfy
`_SAFE_FILENAME`. Admission therefore comes from the manifest: a file is indexable
only when the manifest names it AND its content hash matches.

The lowercase-slug rule stays in force for everything else. `_SAFE_FILENAME` exists
to stop an unlabeled person name (`Jane-Doe.md`) riding a filename into a chunk id,
and `scan_text` cannot catch a name that carries no self-identifying shape — so an
uppercase name that is NOT in the manifest must still be refused.

Fixtures are built here rather than read from the supplied packet: the packet is
deliberately outside the repository, and a test that skips when it is absent is a
vacuous pass in CI.
"""

import hashlib
from pathlib import Path

import pytest

from rag_eval.run import (
    audit_corpus_against_manifest,
    load_corpus_manifest,
    unsafe_corpus_path_reason,
)


def _write(path: Path, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(path: Path, entries: dict[str, str]) -> Path:
    # `shasum -a 256` output format, so the client's own SHA256SUMS.txt is usable
    # verbatim rather than transcribed into a repo-side copy.
    path.write_text(
        "".join(f"{d}  {n}\n" for n, d in entries.items()), encoding="utf-8"
    )
    return path


POLICY_BODY = "# Adverse Action\n\n## Notification timing\n\nNotify within 30 days.\n"


def test_manifest_admits_uppercase_corpus_name(tmp_path: Path):
    # The whole point: SYN-POL-ADVERSE-ACTION.md is not a lowercase slug, and
    # renaming it would break the client's checksum manifest.
    base = tmp_path / "policies"
    digest = _write(base / "SYN-POL-ADVERSE-ACTION.md", POLICY_BODY)
    mf = _manifest(tmp_path / "SHA256SUMS.txt", {"SYN-POL-ADVERSE-ACTION.md": digest})

    manifest = load_corpus_manifest(mf)
    assert (
        unsafe_corpus_path_reason(
            Path("SYN-POL-ADVERSE-ACTION.md"), base=base, manifest=manifest
        )
        is None
    )


def test_manifest_entry_with_wrong_hash_refused(tmp_path: Path):
    # A name-only allowlist would let the approved filename carry different
    # content — the same hole `_LEGACY_DUMP_SHA256` was pinned to close.
    base = tmp_path / "policies"
    _write(base / "SYN-POL-ADVERSE-ACTION.md", POLICY_BODY)
    mf = _manifest(tmp_path / "SHA256SUMS.txt", {"SYN-POL-ADVERSE-ACTION.md": "0" * 64})

    manifest = load_corpus_manifest(mf)
    assert (
        unsafe_corpus_path_reason(
            Path("SYN-POL-ADVERSE-ACTION.md"), base=base, manifest=manifest
        )
        == "manifest-digest-mismatch"
    )


def test_uppercase_person_name_refused_when_not_in_manifest(tmp_path: Path):
    # The control that admitting uppercase names must not weaken. JANE-DOE.md has
    # no self-identifying shape for scan_text, so the manifest is the only thing
    # standing between that name and a chunk id.
    base = tmp_path / "policies"
    digest = _write(base / "SYN-POL-ADVERSE-ACTION.md", POLICY_BODY)
    _write(base / "JANE-DOE.md", POLICY_BODY)
    mf = _manifest(tmp_path / "SHA256SUMS.txt", {"SYN-POL-ADVERSE-ACTION.md": digest})

    manifest = load_corpus_manifest(mf)
    assert (
        unsafe_corpus_path_reason(Path("JANE-DOE.md"), base=base, manifest=manifest)
        == "not-in-manifest"
    )


def test_pii_shaped_name_refused_even_when_manifest_lists_it(tmp_path: Path):
    # scan_text runs first and unconditionally: a manifest cannot approve a name
    # that is itself borrower data. This is the client's second exclusion fixture.
    base = tmp_path / "policies"
    name = "PERSON-ALPHA-ssn-000-00-0000.md"
    digest = _write(base / name, POLICY_BODY)
    mf = _manifest(tmp_path / "SHA256SUMS.txt", {name: digest})

    manifest = load_corpus_manifest(mf)
    assert unsafe_corpus_path_reason(Path(name), base=base, manifest=manifest) == "pii"


def test_audit_fails_on_manifest_entry_with_no_file(tmp_path: Path):
    # A stale allowlist entry must fail, not pass silently — the manifest is the
    # declaration of the approved corpus, so a missing file means the corpus on
    # disk is not the corpus that was approved.
    base = tmp_path / "policies"
    digest = _write(base / "SYN-POL-ADVERSE-ACTION.md", POLICY_BODY)
    mf = _manifest(
        tmp_path / "SHA256SUMS.txt",
        {"SYN-POL-ADVERSE-ACTION.md": digest, "SYN-POL-LOAN-REVIEW.md": "1" * 64},
    )

    problems = audit_corpus_against_manifest(base, load_corpus_manifest(mf))
    assert any("missing" in p for p in problems), problems


def test_audit_fails_on_file_absent_from_manifest(tmp_path: Path):
    # Completeness in the other direction: an extra file on disk is not approved
    # corpus, and an audit that only checks the manifest's own entries would
    # report success over it.
    base = tmp_path / "policies"
    digest = _write(base / "SYN-POL-ADVERSE-ACTION.md", POLICY_BODY)
    _write(base / "SYN-POL-UNAPPROVED.md", POLICY_BODY)
    mf = _manifest(tmp_path / "SHA256SUMS.txt", {"SYN-POL-ADVERSE-ACTION.md": digest})

    problems = audit_corpus_against_manifest(base, load_corpus_manifest(mf))
    assert any("unlisted" in p for p in problems), problems


def test_audit_clean_on_exact_match(tmp_path: Path):
    base = tmp_path / "policies"
    entries = {}
    for name in ("SYN-POL-ADVERSE-ACTION.md", "SYN-POL-LOAN-REVIEW.md"):
        entries[name] = _write(base / name, POLICY_BODY)
    mf = _manifest(tmp_path / "SHA256SUMS.txt", entries)

    assert audit_corpus_against_manifest(base, load_corpus_manifest(mf)) == []


def test_empty_manifest_is_not_a_clean_audit(tmp_path: Path):
    # A verifier must not report success for a path on which it verified nothing:
    # an empty manifest against a populated directory is a failed audit, and
    # against an empty directory it is still not an approved corpus.
    base = tmp_path / "policies"
    base.mkdir(parents=True)
    mf = _manifest(tmp_path / "SHA256SUMS.txt", {})

    assert audit_corpus_against_manifest(base, load_corpus_manifest(mf)) != []


def test_load_corpus_manifest_rejects_malformed(tmp_path: Path):
    mf = tmp_path / "SHA256SUMS.txt"
    mf.write_text("not-a-digest  SYN-POL-ADVERSE-ACTION.md\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_corpus_manifest(mf)


def test_chunk_id_doc_component_is_lowercased(tmp_path: Path):
    # An approved corpus admitted by manifest still yields chunk ids the gold-query
    # validator accepts: `_CHUNK_ID` is lowercase-only, so an uppercase filename
    # would produce ids no `expected` entry could reference.
    from rag_eval.chunker import chunk_markdown
    from rag_eval.run import _CHUNK_ID

    doc = tmp_path / "SYN-POL-ADVERSE-ACTION.md"
    doc.write_text(POLICY_BODY, encoding="utf-8")

    chunks = chunk_markdown(doc)
    assert chunks
    for c in chunks:
        assert _CHUNK_ID.fullmatch(c.chunk_id), c.chunk_id
    assert any(c.chunk_id == "syn-pol-adverse-action#notification-timing" for c in chunks)


def test_chunk_id_collision_across_case_is_refused(tmp_path: Path):
    # Lowercasing the doc component makes two filenames that differ only by case
    # collide. The chunker already fails loud on a duplicate id within a file;
    # across files the manifest is what stops it, since only one spelling can be
    # listed with a matching digest.
    from rag_eval.run import load_corpus_manifest, unsafe_corpus_path_reason

    base = tmp_path / "policies"
    digest = _write(base / "SYN-POL-ADVERSE-ACTION.md", POLICY_BODY)
    _write(base / "syn-pol-adverse-action.md", POLICY_BODY)
    mf = _manifest(tmp_path / "SHA256SUMS.txt", {"SYN-POL-ADVERSE-ACTION.md": digest})
    manifest = load_corpus_manifest(mf)

    assert (
        unsafe_corpus_path_reason(
            Path("SYN-POL-ADVERSE-ACTION.md"), base=base, manifest=manifest
        )
        is None
    )
    assert (
        unsafe_corpus_path_reason(
            Path("syn-pol-adverse-action.md"), base=base, manifest=manifest
        )
        == "not-in-manifest"
    )
