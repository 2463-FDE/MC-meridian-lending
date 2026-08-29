"""Policy retrieval over the committed corpus (ADR 0019).

The assistant's `search_policy` tool calls `search()`. The model chooses the query; this
module decides what is true and hands back the corpus text VERBATIM for the officer. The
model never sees that text — the loop feeds it only the status code and the score (ADR 0019
decision 3), which is why nothing here is shaped for a prompt.

Everything fails closed to an abstention: an unimportable `rag_eval`, no corpus, a corpus
file the ADR 0007 hygiene scan refuses, an unset or malformed threshold, an empty query, an
embedder that cannot be built or called, or a best match below the threshold all return
`PolicyAnswer(status="policy_abstain", ...)`. An officer then reads "no policy
match", which is honest; a fabricated or unvouched-for quotation would not be.

"Everything" is load-bearing, not a summary: retrieval is an optional assistant tool, so no
failure in it may reach the officer's request as a 500 (`assistant.entry`, app/main.py, would
also attach the exception string to the span). Every call into `rag_eval` from here is
guarded.

The index is built once per process from `rag_eval` (importable in the container since card
G2a) and kept in memory — 9 chunks, exact cosine (ADR 0007 rule 6, debt D16).
"""

import threading
from dataclasses import dataclass
from pathlib import Path

try:
    from rag_eval.chunker import chunk_markdown
    from rag_eval.hygiene import scan_file
    from rag_eval.index import InMemoryIndex
    from rag_eval.run import (
        audit_corpus_against_manifest,
        corpus_doc_id,
        load_corpus_manifest,
        make_embedder,
        unsafe_corpus_path_reason,
    )
except ImportError as exc:
    # `rag_eval` is repo-root, and card G2a puts it in the IMAGE (copied to /app/rag_eval,
    # WORKDIR /app) — it does not put it on the sys.path of every checkout. A bare
    # `python -c "import app.main"` from this directory has neither the image layout nor
    # pytest.ini's `pythonpath = ../..`, which is exactly what CI's `backend` import smoke
    # runs. Importing origination must not depend on the assistant's optional retrieval
    # tool, so record the failure and abstain at call time instead of taking the service
    # down. rag-eval-import-gate proves the import inside the shipped image, so a
    # container that silently lost the harness still fails CI.
    _HARNESS_IMPORT_ERROR: ImportError | None = exc
else:
    _HARNESS_IMPORT_ERROR = None

from . import config
from .logging_config import get_logger

log = get_logger("policy_retrieval")

# Abstention reasons. Operational codes for the log and the tests — the model sees only
# `status`, so these never cross the LLM boundary.
# The officer-selectable policy topics, one per retrievable section of the committed
# corpus. This is a CLOSED vocabulary, and it is the officer's whole channel into
# retrieval: the boundary masks free text (an officer question would arrive at the model
# as a redaction placeholder, which is why one was never plumbed), and an enum code
# passes it intact. Each code is a compound operational token, so none can collide with
# a plausible bare name — the property `_SAFE_CATEGORICAL` depends on.
#
# Tied to the corpus, not invented: `test_policy_topic.py` asserts every code here
# actually retrieves a hit, so a topic cannot survive the section behind it being
# renamed or dropped. Adding a code without corpus text to back it fails that test.
#
# Duplicated into `llm/request_builder._SAFE_CATEGORICAL` rather than imported, with a
# parity test: `app/llm/` deliberately depends on nothing in the app domain, and
# importing this module there would drag `rag_eval` into the redaction path.
POLICY_TOPICS = (
    "fee_schedule",
    "apr_finance_charge",
    "interest_rate",
    "eligibility_rules",
    "credit_decisioning",
    "adverse_action",
    "debt_to_income",
    "records_retention",
)

NO_THRESHOLD = "threshold_unset"
NO_CORPUS = "corpus_unavailable"
EMPTY_QUERY = "empty_query"
BELOW_THRESHOLD = "below_threshold"
HARNESS_UNAVAILABLE = "harness_unavailable"
DECISION_TASK = "refused_on_decision_task"

_HIT = "policy_hit"
_ABSTAIN = "policy_abstain"


@dataclass(frozen=True)
class PolicyAnswer:
    """One retrieval result. `text`/`chunk_id` are officer-facing only."""

    status: str
    score: float = 0.0
    chunk_id: str = ""
    text: str = ""
    reason: str = ""

    @property
    def is_hit(self) -> bool:
        return self.status == _HIT

    def tool_result(self) -> dict:
        """The projection the model may see: an allowlisted status and a number.

        Deliberately excludes chunk_id and text. `_SAFE_CATEGORICAL` would mask the prose
        anyway, but relying on the mask would make the boundary an accident of the redactor
        rather than a property of this contract.
        """
        return {"status": self.status, "score": round(self.score, 4)}


def abstain(reason: str, score: float = 0.0) -> PolicyAnswer:
    return PolicyAnswer(status=_ABSTAIN, score=score, reason=reason)


_lock = threading.Lock()
_index_state = None  # (InMemoryIndex, embedder, {chunk_id: text}) once built

# `rag_eval.run`'s pre-admission audit (the client's whole-package delivery
# check) reads manifest entries as `policies/X.md`, matched against the
# delivered package root. This runtime audits directly against the policy
# corpus directory and has always read entries as bare `X.md`. A client's own
# checksum file — the artifact manifest admission exists to admit VERBATIM —
# arrives in the first form, so requiring a human to strip the prefix before
# it reaches POLICY_CORPUS_MANIFEST is exactly the manual-transcription risk
# this control was built to avoid.
_MANIFEST_SUBTREE_PREFIX = "policies/"


def _corpus_relative_manifest(manifest: dict[str, str], base: Path) -> dict[str, str]:
    """Accept a manifest in either convention without a manual edit.

    If any entry carries the `policies/` prefix, the manifest is a
    whole-package listing: every prefixed entry is stripped to corpus-relative
    and a sibling subtree entry (`kb_dump/x.md` — it names its own subtree, so
    it cannot be mistaken for ours) is dropped as out of scope for this
    corpus. A manifest with no prefixed entries is already corpus-relative and
    passes through unchanged — the pre-existing convention, still exercised by
    every manifest fixture that predates this function.

    A bare entry with no subtree qualifier at all (`fee_schedule.md`)
    alongside a `policies/`-prefixed one is ambiguous ONLY when a file by that
    exact name actually sits in the policy corpus directory (`base`): that is
    indistinguishable from an already-narrowed corpus-relative approval an
    operator forgot to convert when appending a new entry in the other
    convention. Silently keeping only the prefixed half would then drop that
    approval's entry from the audited set without dropping the file from
    disk, which the existing "unlisted file" refusal reports as if the file
    were never approved at all — true, but for the wrong reason, and every
    other entry in the manifest is refused right along with it. Raise instead
    so the failure names the actual cause.

    A bare entry that names no file in the corpus directory cannot be that —
    there is no corpus file it could be narrowing an approval for. It is
    package-level metadata (`PACKAGE-INVENTORY.txt`, the checksum file's own
    name) sitting beside the `policies/` subtree in a whole-package delivery,
    exactly like a `kb_dump/`-qualified sibling, so it is dropped the same
    way rather than refusing the whole corpus.
    """
    prefixed = {
        name[len(_MANIFEST_SUBTREE_PREFIX) :]: digest
        for name, digest in manifest.items()
        if name.startswith(_MANIFEST_SUBTREE_PREFIX)
    }
    if not prefixed:
        return manifest
    ambiguous = sorted(
        name for name in manifest if "/" not in name and (base / name).is_file()
    )
    if ambiguous:
        raise ValueError(
            f"manifest mixes corpus-relative and {_MANIFEST_SUBTREE_PREFIX}-prefixed "
            f"entries: {len(ambiguous)} entries have no subtree qualifier at all "
            "(names withheld — an unapproved name can itself be the identifier)"
        )
    return prefixed


def corpus_dir() -> Path:
    """The corpus directory: configured, else the mount, else the checkout.

    /app/policies is where docker-compose.yml bind-mounts ./policies read-only (WORKDIR is
    /app); <repo>/policies is where it lives in a checkout, which is what the tests read.
    """
    if config.POLICY_CORPUS_DIR:
        return Path(config.POLICY_CORPUS_DIR)
    parents = Path(__file__).resolve().parents
    # parents[1] is /app in the image and services/origination-service in a checkout;
    # parents[3] is the repo root, which EXISTS ONLY in the checkout — the image bottoms
    # out at / after three levels, so indexing it unguarded raises IndexError there.
    candidates = [parents[1] / "policies"]
    if len(parents) > 3:
        candidates.append(parents[3] / "policies")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def reset_index_cache() -> None:
    """Drop the built index (tests, and any future corpus reload)."""
    global _index_state
    with _lock:
        _index_state = None


def _load_corpus() -> list:
    """Chunk every corpus document the hygiene gate passes.

    ADR 0007 gates the COMMITTED corpus in CI; this scan covers the copy that actually
    arrives, since it comes over a bind mount that a deployment could point elsewhere. A
    file with a finding is skipped and named in the log by path and PII type — never by
    value, which is the whole point of the scan.

    A file that cannot be read or chunked is skipped for the same reason: an unreadable
    corpus file must not take the assistant down, and it must not be quoted either.
    """
    chunks = []
    base = corpus_dir()
    manifest = None
    if config.POLICY_CORPUS_MANIFEST:
        # Fail closed on every manifest problem: an unreadable or malformed
        # declaration, or a corpus that does not match it, yields no corpus and
        # therefore an abstention. Indexing the listed subset of a mismatched
        # directory would serve a different corpus than the one approved, which
        # is worse than answering "no policy match".
        try:
            manifest = load_corpus_manifest(Path(config.POLICY_CORPUS_MANIFEST))
            manifest = _corpus_relative_manifest(manifest, base)
        except (OSError, ValueError) as exc:
            log.warning("policy corpus manifest unusable, indexing nothing: %s", exc)
            return []
        problems = audit_corpus_against_manifest(base, manifest)
        if problems:
            log.warning(
                "policy corpus does not match its manifest, indexing nothing: %s",
                "; ".join(problems),
            )
            return []
    if manifest is not None:
        # The audit compares path SETS; it never reads content. Re-check every
        # approved entry's digest here, at the moment the corpus is about to be
        # read — `rag_eval.run` does exactly this after its own clean audit, and
        # aborts the run. It is also the ONLY place a non-markdown entry is
        # verified at all, since the walk below is markdown-only: a listed .txt
        # mutated after approval would otherwise leave the corpus looking
        # approved. A corpus that no longer matches its declaration is not the
        # corpus that was approved, so this refuses all of it rather than
        # skipping the offending file and serving the rest.
        unapproved = [
            reason
            for name in manifest
            if (
                reason := unsafe_corpus_path_reason(
                    Path(name), base=base, manifest=manifest
                )
            )
            is not None
        ]
        if unapproved:
            log.warning(
                "policy corpus does not match its manifest on content, indexing "
                "nothing: %s of %s approved entries (%s) — names withheld, an "
                "unapproved name can itself be the identifier",
                len(unapproved),
                len(manifest),
                ", ".join(sorted(set(unapproved))),
            )
            return []

        # The audit grades every file the manifest lists; the loader only chunks
        # markdown. An approved file in another format therefore audits clean and
        # is then never indexed — the same silent coverage hole a flat walk
        # created for subdirectories. Say so: an officer getting "no policy
        # match" on approved content otherwise has nothing to read. Count only,
        # never the names — a manifest can list a filename this path never ran
        # `scan_text` over, and the name itself can be the borrower data.
        unindexable = sum(1 for name in manifest if not name.endswith(".md"))
        if unindexable:
            log.warning(
                "policy corpus manifest approves %s file(s) this loader cannot "
                "index (not .md), which are not retrievable (names withheld)",
                unindexable,
            )

    # Recursive ONLY under a manifest, to match the walk that declaration is
    # graded by: `audit_corpus_against_manifest` uses rglob (as does
    # `rag_eval.run`'s own discovery), so a flat glob would let an approved file
    # in a subdirectory audit clean and then never be indexed — retrieval
    # abstaining on approved content with nothing reporting a refusal.
    #
    # Without a manifest there is no audit to match, and the corpus arrives over
    # a bind mount an operator controls. Descending there would newly serve
    # whatever sits in a subdirectory — a draft, an archived copy — verbatim to
    # an officer as policy, admitted by nothing but its filename. Stay flat.
    walk = base.rglob("*.md") if manifest is not None else base.glob("*.md")
    for path in sorted(walk):
        # The hygiene gate below scans CONTENT only. A file mounted with clean
        # content but an unsafe NAME (jane-doe-123-45-6789.md) would still pass
        # it, then leak identity through the officer-visible chunk id (doc =
        # path.stem, ADR 0007 rule 6) and this loop's own log lines. ADR 0007's
        # CI-time scan never sees this bind-mounted copy, so re-check the path
        # here with the same function rag_eval.run applies to the committed
        # corpus (rag_eval/run.py::run).
        rel = path.relative_to(base)
        reason = unsafe_corpus_path_reason(rel, base=base, manifest=manifest)
        if reason is not None:
            # The filename itself is the flagged problem — unlike the branches
            # below, path.name is not safe to log here.
            log.warning(
                "policy corpus file REFUSED for an unsafe name (%s), not "
                "indexed (name withheld)",
                reason,
            )
            continue
        # Named by doc id from here down, never `path.name`: under a manifest the
        # filename is admitted on its digest and graded by nothing — `scan_text`
        # is label-gated, so a bare person name passes it — so the name must not
        # reach a log line any more than it reaches a chunk id. Without a
        # manifest the doc id is the stem the slug convention already graded.
        doc_id = corpus_doc_id(rel, manifest)
        try:
            verdict = scan_file(path)
        except OSError as exc:
            log.warning("policy corpus file unreadable, skipped: %s (%s)", doc_id, exc)
            continue
        if not verdict.passed:
            log.warning(
                "policy corpus file REFUSED by the hygiene gate, not indexed: %s (%s)",
                doc_id,
                verdict.counts(),
            )
            continue
        try:
            chunks.extend(chunk_markdown(path, doc_id=doc_id))
        except (OSError, ValueError) as exc:
            log.warning(
                "policy corpus file not chunkable, skipped: %s (%s)", doc_id, exc
            )
    return chunks


def _build_index():
    chunks = _load_corpus()
    if not chunks:
        return None
    # Checked BEFORE any embedder work: it needs no vectors, and on the Bedrock
    # backend every chunk below is a billed provider call for a corpus this is
    # about to refuse anyway.
    #
    # Two documents sharing a filename stem (different directories, or two
    # manifest-approved spellings differing only by case) emit identical chunk
    # ids, because the chunker lowercases the stem. InMemoryIndex keeps both
    # entries while the id->text map keeps one, so search would score one
    # chunk's vector and return the other chunk's text — the wrong policy under
    # a citation that looks legitimate. `rag_eval.run` aborts the run on this;
    # the service's equivalent is to index nothing, which abstains.
    ids = [c.chunk_id for c in chunks]
    duplicates = sorted({cid for cid in ids if ids.count(cid) > 1})
    if duplicates:
        log.warning(
            "policy corpus has duplicate chunk ids, indexing nothing: %s "
            "— two documents share a filename stem",
            duplicates,
        )
        return None
    try:
        embedder = make_embedder()
        embedder.fit([c.text for c in chunks])
        index = InMemoryIndex()
        for chunk in chunks:
            index.add(chunk.chunk_id, embedder.embed(chunk.text))
    except Exception as exc:
        # ONE guard over the whole build, deliberately: `make_embedder` reads
        # RAG_EMBEDDER and AWS_REGION (both wired through docker-compose.yml)
        # and raises on an unknown backend name or a region that was never
        # configured, while the per-chunk `embed` below is a provider call on
        # the Bedrock backend and fails on absent, expired or rotated
        # credentials. Guarding only the first two missed that loop:
        # `BedrockEmbedder.fit` is a no-op that just sets the signature, so the
        # FIRST network call is inside the loop, and a container smoke with a
        # real region and no credentials raised
        # `ClientError(IncompleteSignatureException)` out of `search()`.
        #
        # None of it is an unhealthy service — retrieval is an optional
        # assistant tool, so it abstains, exactly as an unset threshold and an
        # absent harness already do. Unguarded, the exception left `search()`,
        # crossed `assistant.entry` (app/main.py, which attaches
        # `str(exception)` to the span) and became a 500 on the officer's
        # request.
        #
        # The message is safe to log: it is config text or a provider fault, and
        # the only input in scope here is gate-passed corpus text an officer may
        # already read. The model-supplied query is NOT in scope — see
        # `search()` for the embed that holds one, which logs the type alone.
        log.warning(
            "policy retrieval disabled: embedder unavailable (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return None
    log.info("policy corpus indexed: %s chunks from %s", len(chunks), corpus_dir())
    return index, embedder, {c.chunk_id: c.text for c in chunks}


def _index():
    """The process-wide index, built on first use. None when there is no usable corpus."""
    global _index_state
    if _index_state is not None:
        return _index_state
    with _lock:
        if _index_state is None:
            _index_state = _build_index()
        return _index_state


def search(query: str) -> PolicyAnswer:
    """Best corpus match for `query`, or an abstention.

    The query is model-supplied free text and stays in this process: it is never logged
    (it could carry whatever the model put in it) and never returned to the model.
    """
    threshold = config.policy_retrieval_min_score()
    if threshold is None:
        log.warning(
            "policy retrieval disabled: POLICY_RETRIEVAL_MIN_SCORE is unset or unusable "
            "— every query abstains (ADR 0019)"
        )
        return abstain(NO_THRESHOLD)
    if not isinstance(query, str) or not query.strip():
        return abstain(EMPTY_QUERY)
    if _HARNESS_IMPORT_ERROR is not None:
        log.warning(
            "policy retrieval disabled: rag_eval is not importable (%s) — every query "
            "abstains (ADR 0019)",
            _HARNESS_IMPORT_ERROR,
        )
        return abstain(HARNESS_UNAVAILABLE)
    state = _index()
    if state is None:
        log.warning(
            "policy retrieval disabled: no usable corpus under %s", corpus_dir()
        )
        return abstain(NO_CORPUS)
    index, embedder, texts = state
    try:
        vector = embedder.embed(query)
    except Exception as exc:
        # On the Bedrock backend this is a provider call holding the
        # model-authored query. Log the exception TYPE only: a fault that echoes
        # the input it was handed would put that free text in the log, and then
        # -- raised -- on the `assistant.entry` span too. Abstaining keeps both
        # promises at once (the query never leaves this process, and a provider
        # fault is an abstention rather than a 500).
        log.warning(
            "policy retrieval abstained: query embedding failed (%s) — message "
            "withheld, it can carry the model-authored query",
            type(exc).__name__,
        )
        return abstain(NO_CORPUS)
    hits = index.search(vector, k=1)
    if not hits:
        return abstain(NO_CORPUS)
    chunk_id, score = hits[0]
    if score < threshold:
        log.info(
            "policy retrieval abstained: best match %s scored %.4f, below %.4f",
            chunk_id,
            score,
            threshold,
        )
        return abstain(BELOW_THRESHOLD, score=score)
    return PolicyAnswer(
        status=_HIT, score=score, chunk_id=chunk_id, text=texts[chunk_id]
    )
