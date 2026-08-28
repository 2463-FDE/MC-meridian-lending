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
import logging
from pathlib import Path

from app import config, policy_retrieval
from rag_eval.run import corpus_doc_id

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
    # The id is digest-derived, not the stem: under manifest admission the
    # filename is graded by nothing, so it never becomes an officer-visible id.
    doc_id = corpus_doc_id(Path(NAME), digests)
    assert all(c.chunk_id.startswith(f"{doc_id}#") for c in chunks)


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
    nested = next(k for k in digests if k.endswith("SYN-POL-DEEP.md"))
    assert any(
        cid.startswith(f"{corpus_doc_id(Path(nested), digests)}#") for cid in ids
    )


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


def test_same_stem_in_two_directories_no_longer_collides(tmp_path: Path, monkeypatch):
    # A digest-derived doc id ends the stem collision structurally: two approved
    # documents with DIFFERENT content can no longer share a chunk id, so neither
    # is refused and neither can be served under the other's citation.
    base, digests = _twin_corpus(tmp_path, SAME_STEM)
    _configure(monkeypatch, base, _manifest(tmp_path, digests))

    chunks = policy_retrieval._load_corpus()
    assert len(chunks) == len({c.chunk_id for c in chunks})
    assert policy_retrieval._build_index() is not None


def test_case_variant_pair_no_longer_collides(tmp_path: Path, monkeypatch):
    base, digests = _twin_corpus(tmp_path, CASE_TWIN)
    if len(list(base.glob("*.md"))) < 2:
        import pytest

        pytest.skip("case-insensitive filesystem cannot hold both spellings")
    _configure(monkeypatch, base, _manifest(tmp_path, digests))

    chunks = policy_retrieval._load_corpus()
    assert len(chunks) == len({c.chunk_id for c in chunks})
    assert policy_retrieval._build_index() is not None


def test_byte_identical_approved_twins_index_nothing(tmp_path: Path, monkeypatch):
    # The duplicate-id guard still has a job: two approved files with identical
    # CONTENT hash the same, so they share a doc id. Both chunks then carry the
    # same text, but the guard stays fail-closed rather than reasoning about it.
    base, digests = _corpus(tmp_path)
    twin = base / "archive" / NAME
    twin.parent.mkdir(parents=True, exist_ok=True)
    twin.write_text(BODY, encoding="utf-8")
    digests[f"archive/{NAME}"] = hashlib.sha256(twin.read_bytes()).hexdigest()
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


class _CaptureHandler(logging.Handler):
    """Collect records emitted on the policy_retrieval logger. `logging_config.get_logger`
    sets `propagate = False`, so `caplog` reports "nothing was logged" for a line that WAS
    logged -- a false green on the very report under test (same reason test_authz.py and
    test_llm_client.py each carry their own copy)."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_manifest_approved_non_markdown_is_reported_not_silent(
    tmp_path: Path, monkeypatch
):
    # The audit grades every listed file; the loader chunks markdown only. An
    # approved .txt therefore audits clean and is never indexed -- which must not
    # be silent, or an officer gets "no policy match" on approved content with
    # nothing explaining why. The count is logged, never the names: this path
    # never ran scan_text over them.
    base, digests = _corpus(tmp_path)
    other = base / "SYN-POL-PLAIN.txt"
    other.write_text("Approved plain-text policy: fee cap is 5%.", encoding="utf-8")
    digests["SYN-POL-PLAIN.txt"] = hashlib.sha256(other.read_bytes()).hexdigest()
    _configure(monkeypatch, base, _manifest(tmp_path, digests))

    handler = _CaptureHandler()
    logger = logging.getLogger("policy_retrieval")
    logger.addHandler(handler)
    try:
        chunks = policy_retrieval._load_corpus()
    finally:
        logger.removeHandler(handler)

    # The markdown half still indexes -- this is a report, not a refusal.
    assert any(
        c.chunk_id.startswith(f"{corpus_doc_id(Path(NAME), digests)}#") for c in chunks
    )
    messages = [r.getMessage() for r in handler.records]
    assert any("cannot index" in m for m in messages)
    # The name is withheld: a manifest can list a filename this path never
    # scanned, and the name itself can be the borrower data.
    assert not any("SYN-POL-PLAIN" in m for m in messages)


def test_mutated_approved_non_markdown_forces_abstention(tmp_path: Path, monkeypatch):
    # The audit compares path SETS only. A .txt is never walked by the markdown
    # loop, so without a content re-check nothing ever hashes it: an approved
    # file mutated after approval would leave the corpus looking approved and the
    # service would keep serving the rest of it.
    base, digests = _corpus(tmp_path)
    other = base / "SYN-POL-PLAIN.txt"
    other.write_text("Approved plain-text policy: fee cap is 5%.", encoding="utf-8")
    digests["SYN-POL-PLAIN.txt"] = hashlib.sha256(other.read_bytes()).hexdigest()
    _configure(monkeypatch, base, _manifest(tmp_path, digests))

    other.write_text("Fee cap is 95%.", encoding="utf-8")  # mutated after approval
    policy_retrieval.reset_index_cache()

    assert policy_retrieval._load_corpus() == []


def test_mutated_approved_markdown_refuses_the_whole_corpus(
    tmp_path: Path, monkeypatch
):
    # A corpus that no longer matches its declaration is not the corpus that was
    # approved, so the remaining files are not servable either. `rag_eval.run`
    # aborts the run on this; skipping the one file and indexing the rest served
    # an approved-looking corpus out of a violated declaration.
    base, digests = _corpus(tmp_path)
    second = base / "SYN-POL-FEES.md"
    second.write_text("# Fees\n\n## Late\n\nLate fee is $35.\n", encoding="utf-8")
    digests["SYN-POL-FEES.md"] = hashlib.sha256(second.read_bytes()).hexdigest()
    _configure(monkeypatch, base, _manifest(tmp_path, digests))

    second.write_text("# Fees\n\n## Late\n\nLate fee is $3500.\n", encoding="utf-8")
    policy_retrieval.reset_index_cache()

    assert policy_retrieval._load_corpus() == []


def test_manifest_admitted_person_name_never_reaches_a_chunk_id(
    tmp_path: Path, monkeypatch, caplog
):
    # Manifest admission grades the DIGEST, so the lowercase-slug rule that keeps
    # an unlabeled person name out of an officer-visible chunk id never runs.
    # `scan_text` does not cover it either — it is label-gated, so a bare
    # `Jane-Doe.md` carries no self-identifying shape and passes. The name must
    # not become the doc id, and must not reach this loader's own log lines.
    base, digests = _corpus(tmp_path, names=("Jane-Doe.md",))
    _configure(monkeypatch, base, _manifest(tmp_path, digests))

    with caplog.at_level(logging.WARNING):
        chunks = policy_retrieval._load_corpus()

    assert chunks, "an approved document must stay retrievable, not be refused"
    for c in chunks:
        assert "jane" not in c.chunk_id.lower()
        assert "doe" not in c.chunk_id.lower()
    assert "jane" not in caplog.text.lower()
