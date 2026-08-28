"""Eval harness runner: gate -> ingest -> embed (cached) -> retrieve -> report.

One command, zero LLM calls (spec D1.1): ``python -m rag_eval.run``.

The hygiene gate is a hard precondition enforced here in code (spec D2.4,
ADR 0007 rule 4): chunks are only ever produced from gate-passed files inside
``run()`` — there is no other path into the embedder, and no override flag.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

from rag_eval import report as report_mod
from rag_eval.cache import EmbeddingCache
from rag_eval.chunker import Chunk, _slug, chunk_markdown
from rag_eval.embedder import BedrockEmbedder, TfidfEmbedder
from rag_eval.hygiene import FileVerdict, scan_file, scan_text
from rag_eval.index import InMemoryIndex
from rag_eval.metrics import (
    UNMAPPED,
    UNSCORABLE_CLASS,
    Aggregate,
    QueryEval,
    aggregate,
)

GOLD_PATH = Path(__file__).parent / "gold_queries.json"

# Benign VCS/OS metadata skipped during corpus discovery. Kept deliberately
# narrow: any OTHER dot-prefixed file (e.g. .customers.csv) is scanned like a
# normal corpus file, so a hidden data dump cannot bypass the gate.
_SKIP_NAMES = {".gitkeep", ".gitignore", ".gitattributes", ".ds_store"}

# A safe corpus filename: lowercase-kebab/snake slug plus dots for extensions.
# Uppercase letters and spaces are refused so an unlabeled name/address
# ("Jane-Doe.md", "123 Main St.txt") cannot ride in a filename into a chunk id
# or the report (see run() gate). A leading dot IS allowed — dot-prefixed data
# files are deliberately still content-scanned (see _SKIP_NAMES note), so the
# name convention must not short-circuit that path.
_SAFE_FILENAME = re.compile(r"\.?[a-z0-9][a-z0-9._-]*")


_MANIFEST_DIGEST = re.compile(r"[0-9a-f]{64}")


def load_corpus_manifest(path: Path) -> dict[str, str]:
    """Read a corpus manifest in `shasum -a 256` format: digest, then filename.

    The manifest is the declaration of WHICH corpus was approved and WHAT its
    content was. Names are corpus-directory-relative, so a client packet whose
    checksum file covers the whole delivery is narrowed to its policy directory
    once, under human review, rather than reinterpreted on every run.

    Malformed input raises: a manifest that cannot be parsed must not degrade to
    an empty allowlist, which would refuse the whole corpus and read as "the
    corpus is contaminated" instead of "the manifest is broken".
    """
    entries: dict[str, str] = {}
    for lineno, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip()
        if not line:
            continue
        # shasum writes two spaces (text mode) or " *" (binary mode).
        digest, sep, name = line.partition("  ")
        if not sep:
            digest, sep, name = line.partition(" *")
        name = name.strip()
        if not sep or not name or not _MANIFEST_DIGEST.fullmatch(digest):
            raise ValueError(
                f"corpus manifest line {lineno} is not `<sha256>  <filename>`"
            )
        entries[name] = digest
    return entries


def audit_corpus_against_manifest(
    base: Path,
    manifest: dict[str, str],
    subtree: str | None = None,
    manifest_file: Path | None = None,
) -> list[str]:
    """Problems making the corpus on disk differ from the approved manifest.

    Audits BOTH directions. Checking only the manifest's own entries would report
    success over an unlisted file sitting in the corpus directory, which is the
    failure mode a hand-maintained allowlist always has: it grades what it lists
    and stays silent about what it does not.

    Findings name a manifest entry (approved text, safe to echo) but never an
    unlisted filename — an unapproved name can itself be the PII, so it is
    reported by position, as run()'s own refusal path does.

    `manifest_file` names the checksum file itself when it sits inside the
    audited root (a flat package with no subtree to exclude it by, unlike the
    policy corpus's manifest at `base`, one level above the scoped `policies/`
    subtree). The manifest never lists its own path, so without this it would
    always report itself as one unlisted file.
    """
    if not manifest:
        return ["manifest declares no approved corpus files"]
    # `subtree` scopes BOTH sides — the files graded and the manifest entries
    # considered — with keys staying relative to `base`. Scoping only the files
    # would report every entry outside the subtree as missing, which is what a
    # supplied delivery's own checksum file looks like: it covers the whole
    # package, of which the policy corpus is one directory. run() also scans
    # kb_dump, which a policy manifest does not govern (it has its own pinned
    # exception), so grading every root would conflate two separate controls.
    root = base / subtree if subtree else base
    if not root.is_dir():
        return [f"corpus directory does not exist: {subtree or '.'}"]
    prefix = f"{subtree}/" if subtree else ""
    scoped = {k: v for k, v in manifest.items() if k.startswith(prefix)}
    if not scoped:
        # A verifier must not report success for a path on which it verified
        # nothing: a subtree no manifest entry covers is an unapproved corpus, not
        # a clean one.
        return [f"manifest declares no approved files under {subtree or '.'}"]
    on_disk = {p.relative_to(base).as_posix() for p in root.rglob("*") if p.is_file()}
    if manifest_file is not None:
        on_disk.discard(manifest_file.relative_to(base).as_posix())
    problems = []
    for name in sorted(set(scoped) - on_disk):
        problems.append(f"manifest entry missing on disk: {name}")
    unlisted = sorted(on_disk - set(scoped))
    for position, _ in enumerate(unlisted, start=1):
        problems.append(
            f"unlisted file in corpus directory at position {position} "
            "(name withheld — an unapproved name can itself be the identifier)"
        )
    # Content, not just presence: a listed name whose bytes were edited after
    # the manifest was pinned is not the approved corpus either. The policy
    # corpus also gets this checked per-file at admission time
    # (unsafe_corpus_path_reason), but nothing else ingests a manifest-governed
    # artifact, so an unlisted-artifact caller (the displayed-summaries package)
    # has no second pass to catch a content edit — this is its only one.
    for name in sorted(set(scoped) & on_disk):
        digest = hashlib.sha256((base / name).read_bytes()).hexdigest()
        if digest != scoped[name]:
            problems.append(
                f"manifest entry does not match its approved digest: {name}"
            )
    return problems


def unsafe_corpus_path_reason(
    rel_path: Path,
    base: Path | None = None,
    manifest: dict[str, str] | None = None,
) -> str | None:
    """Why a corpus-relative path cannot be trusted as a chunk id / log/report entry.

    A file with clean CONTENT can still leak identity through its NAME — the
    chunker turns the stem into a chunk id, and refusal/report paths echo the
    path. Shared by run()'s corpus-relative scan below and by
    origination-service's policy_retrieval, which indexes a bind-mounted
    corpus at runtime that this module's own CI-time scan never sees. Returns
    "pii", "not-in-manifest", "manifest-digest-mismatch" or "non-slug", or None
    when the path is safe. Callers must not echo the path itself when a reason
    comes back — the name can be the PII.

    With no `manifest`, admission is the lowercase-slug convention, which is what
    stops an unlabeled person name (`Jane-Doe.md`) becoming a chunk id when
    `scan_text` finds no self-identifying shape in it.

    With a `manifest`, admission is that declaration instead: the name must be
    listed AND the file's content must hash to the listed digest. This is how a
    supplied corpus whose filenames are not slugs is indexed without renaming it,
    which would invalidate the checksums it was approved under. Name-only would
    reopen the hole `_LEGACY_DUMP_SHA256` was pinned to close — the approved
    filename carrying different content. The `scan_text` check still runs first
    and unconditionally: a manifest cannot approve a name that is borrower data.
    """
    if scan_text(str(rel_path)):
        return "pii"
    if manifest is not None:
        if base is None:
            raise ValueError("manifest admission needs `base` to hash the file")
        listed = manifest.get(rel_path.as_posix())
        if listed is None:
            return "not-in-manifest"
        try:
            digest = hashlib.sha256((base / rel_path).read_bytes()).hexdigest()
        except OSError:
            # Unreadable is not approved: fail closed rather than admit a file
            # whose content could not be checked against the declaration.
            return "manifest-digest-mismatch"
        return None if digest == listed else "manifest-digest-mismatch"
    if not all(_SAFE_FILENAME.fullmatch(part) for part in rel_path.parts):
        return "non-slug"
    return None


def corpus_doc_id(rel_path: Path, manifest: dict[str, str] | None = None) -> str:
    """The officer-visible document id for an admitted corpus file.

    Without a manifest, admission IS the lowercase-slug convention — the name has
    already been graded by `unsafe_corpus_path_reason`, so the stem is safe to
    show and the gold set's `expected` entries depend on it staying readable.

    With a manifest, admission is the listed digest and the NAME is graded by
    nothing: `_SAFE_FILENAME` never runs on that branch, and `scan_text` is
    label-gated, so a bare `Jane-Doe.md` passes and would otherwise become the
    chunk id `jane-doe#...` — a person name in citations, logs and report ids. No
    structural rule separates a person name from a policy code, so the name is not
    used at all here: the id derives from the approved digest, which is
    deterministic across runs, carries no identity, and cannot be inverted to the
    filename. Readability survives in the chunk text, which the chunker prefixes
    with the document title and section.

    Raises ValueError (never echoing the path — the name can be the PII) when the
    manifest does not list the file; callers refuse an unlisted file before here.
    """
    if manifest is None:
        return rel_path.stem.lower()
    listed = manifest.get(rel_path.as_posix())
    if listed is None:
        raise ValueError("cannot derive a doc id: file is not in the manifest")
    return f"doc-{listed[:12]}"


# Gold-query STRUCTURED fields are locked to machine shapes so they cannot carry
# free-text PII: an id is a slug, an expected entry is a chunk id (doc#section,
# from chunker.py). That leaves only the natural-language query/note as free
# text — screened by scan_text for self-identifying PII (SSN/PAN/email) AND by
# _looks_like_person_name below for a probable applicant name, per the
# gold_queries.json contract that queries describe synthetic scenarios only.
# (Legitimate multi-word proper nouns like "Fair Credit Reporting Act" are
# allowlisted so the name guard doesn't refuse them.)
_GOLD_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
_CHUNK_ID = re.compile(r"[a-z0-9][a-z0-9._-]*#[a-z0-9._-]+")
# The officer topic vocabulary, duplicated from POLICY_TOPICS in
# origination-service's policy_retrieval. The harness cannot import a service, so
# a test asserts the two agree rather than trusting the copy — a drifted copy
# would score against codes the officer channel no longer offers.
TOPIC_CODES = (
    "fee_schedule",
    "apr_finance_charge",
    "interest_rate",
    "eligibility_rules",
    "credit_decisioning",
    "adverse_action",
    "debt_to_income",
    "records_retention",
)

# `UNMAPPED` is imported from metrics rather than defined here: it is deliberately
# kept OUT of TOPIC_CODES so it can never be mistaken for something the product can
# be asked, and it is reported on its own line rather than as a topic.

_ALLOWED_GOLD_KEYS = {
    "id",
    "query",
    "expected",
    "unanswerable",
    "note",
    "topic",
    "outcome_class",
    # The frozen anchor, as the client ships it. Preferred over a literal
    # `expected`: a chunk id is not stable across admission modes (see
    # `corpus_doc_id`), so a set carrying literal ids is welded to one mode —
    # and her filenames are non-slug, so manifest admission is the only mode
    # that can index her corpus at all. Deriving the id here keeps the gold
    # set readable and portable.
    "source_document",
    "source_heading",
}

# The anchor halves, as structured fields rather than free text. run() resolves
# them against the ADMITTED corpus — the document must be one the gate
# passed, and the heading must match a section that exists in it, or the load has
# already failed — so neither can carry arbitrary prose. That resolution is what
# lets them sit out the free-text name heuristic (_looks_like_person_name);
# scan_text still covers them for labelled PII.
_ANCHOR_KEYS = frozenset({"source_document", "source_heading"})

# Her delivery names these columns in camelCase, in the CSV and in the
# authoritative JSONL alike, so a gold set transcribed from it keeps that
# spelling. Accept both and normalize BEFORE the unknown-key check, rather than
# refusing the shape her own data ships in.
_GOLD_KEY_ALIASES = {
    "sourceDocument": "source_document",
    "sourceHeading": "source_heading",
}

# The client's four outcome classes. `no_match` is the abstention case the
# harness already scores as `unanswerable`, and carries no expected chunk.
# `answer` and `manager_escalation` are scored on retrieval rank and so must
# each name an expected chunk. `clarification` is UNSCORABLE_CLASS (see
# metrics.py): the officer channel is a closed topic enum with free text
# masked at the boundary, so no ask-back path exists to exercise, and the case
# is excluded from every denominator rather than scored on a target it has no
# way to hit.
_OUTCOME_CLASSES = frozenset(
    {"answer", "manager_escalation", "clarification", "no_match"}
)

# The ONE corpus file ADR 0007 documents as legacy-contaminated: kb_dump is the
# raw pre-remediation dump, so its refusal is expected and is the whole point of
# the hygiene report. EVERY other refusal — a policy doc, or any new file — is a
# fresh PII-in-repo regression and must fail the CI gate (enforced in main()).
_EXPECTED_CONTAMINATED = Path("kb_dump") / "applications.jsonl"

# The exception is pinned to this EXACT content. A path-only allowlist would let
# someone add fresh SSNs/PANs/CVVs to the legacy dump and still exit green (the
# file stays "refused"). Pinning the hash means any change to the dump flips
# this digest and fails the gate closed, forcing explicit human re-approval of
# the new baseline in review. Regenerate after an approved change with:
#   shasum -a 256 kb_dump/applications.jsonl
_LEGACY_DUMP_SHA256 = "38d3ffdc0e85e2ac423173299a4f35efbff73c003adcf59c0745fcae68eb7711"


def _refusal_is_expected(path_str: str, base: Path) -> bool:
    p = Path(path_str)
    # Exact canonical path only — not a parent/name suffix. Recursive scanning
    # would otherwise let a second copy at kb_dump/archive/kb_dump/applications.jsonl
    # inherit the exception and smuggle duplicate PII past the gate.
    if p.resolve() != (base / _EXPECTED_CONTAMINATED).resolve():
        return False
    try:
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return False
    # Expected only at the approved content — a modified dump is treated as a new
    # refusal and fails the gate.
    return digest == _LEGACY_DUMP_SHA256


def _gold_strings(value):
    """Yield every string in a gold-query object (recursing dicts/lists) so the
    PII scan covers id/expected/note, not just the query text."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _gold_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _gold_strings(v)


# Multi-word proper nouns that are legitimately Title-Cased in a lending query
# (regulations, statutes, the lender itself) — allowlisted so the person-name
# guard below does not refuse them. Extend as new legitimate proper nouns appear.
_NAME_ALLOWLIST = (
    "Fair Credit Reporting Act",
    "Equal Credit Opportunity Act",
    "Fair Debt Collection Practices Act",
    "Truth in Lending Act",
    "Consumer Financial Protection Bureau",
    "Military Lending Act",
    "Servicemembers Civil Relief Act",
    "Social Security",
    "Meridian Lending",
)
# A run of two or more consecutive Title-Case words (each an initial capital + a
# lowercase tail) is a probable person name. Single sentence-initial capitals,
# ALL-CAPS acronyms (DTI, NSF), and ids (#6012) do not match, so the 12 real gold
# queries pass clean. Gold queries are a committed input embedded to an external
# API (Bedrock) and printed verbatim into eval_report.md, so a real applicant
# name must fail closed before either — scan_text cannot catch an unlabeled name
# without also refusing regulatory phrases, so this gold-specific guard does.
_PERSON_NAME = re.compile(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+")


def _looks_like_person_name(text: str) -> bool:
    for phrase in _NAME_ALLOWLIST:
        text = text.replace(phrase, " ")
    return bool(_PERSON_NAME.search(text))


# Titan Embed Text v2 — AWS-native, cheap, 1024-dim. Confirm the id is enabled
# in your account/region before relying on it (Bedrock model ids are
# region/account-specific), same caveat as the LLM client's Bedrock model.
_DEFAULT_BEDROCK_MODEL = "amazon.titan-embed-text-v2:0"


def cache_enabled(embedder) -> bool:
    """Whether embedding vectors may be written to disk for this backend.

    The on-disk cache holds vectors derived from corpus content, which the client
    excluded from retention for the graded run. A provider backend therefore runs
    cacheless — it also means a rerun re-embeds, which is why the run is bounded to
    one pass plus one correction.

    The TF-IDF path keeps its cache: it makes no provider call, and the keyless
    local run and the blocking gate both depend on it staying fast.
    """
    # Keyed off the embedder in use, not off RAG_EMBEDDER: a caller that injects
    # an embedder (tests, and any future programmatic use) would otherwise have
    # the retention decision disagree with the backend actually making calls.
    return not getattr(embedder, "IS_PROVIDER_BACKED", True)


def refuse_traced_provider_run(embedder) -> None:
    """Refuse a provider-backed run while LangSmith tracing is on.

    origination-service hardens the LangSmith singleton with hide_inputs and
    hide_outputs, but that hardening deliberately does not hide ERRORS
    (`_hide_run_error` is passthrough) and runs at service startup, which an
    offline harness never reaches. Nothing in the required post-run report comes
    from a trace, so the safe configuration is simply no tracing.
    """
    if not getattr(embedder, "IS_PROVIDER_BACKED", True):
        return
    if os.getenv("LANGSMITH_TRACING", "").strip().lower() in {"1", "true", "yes"}:
        raise ValueError(
            "LANGSMITH_TRACING is enabled and the embedding backend is a provider "
            "— trace error bodies are not hidden, so unset it for the graded run"
        )


def make_embedder():
    """Pick the embedding backend from ``RAG_EMBEDDER`` (default ``tfidf``).

    ``tfidf`` (default) keeps CI keyless and stdlib-only. ``bedrock`` uses
    Amazon Bedrock via boto3 (``RAG_BEDROCK_MODEL``, ``AWS_REGION``, AWS creds)
    — the scaling path. An unknown value fails loud rather than silently
    falling back to a different backend than asked for.

    Blank counts as unset for both variables. docker-compose.yml passes
    ``${RAG_EMBEDDER:-}``, which sets the variable to "" rather than omitting
    it, so a `getenv` default alone never fires on the compose path and the
    keyless default would be unreachable exactly where the corpus is mounted.
    """
    name = os.getenv("RAG_EMBEDDER", "").strip() or "tfidf"
    if name == "tfidf":
        return TfidfEmbedder()
    if name == "bedrock":
        # Validated BEFORE the client is constructed. Passing region=None lets
        # boto3 resolve one itself from environment, profile or instance config —
        # region discovery, which the client excluded. Failing here also keeps the
        # check testable without boto3 installed, so the keyless gate can cover it.
        region = os.getenv("AWS_REGION", "").strip()
        if not region:
            raise ValueError(
                "AWS_REGION must be set explicitly for RAG_EMBEDDER=bedrock — "
                "an unset value lets boto3 discover a region, and Bedrock model "
                "access is granted per region"
            )
        return BedrockEmbedder(
            model_id=os.getenv("RAG_BEDROCK_MODEL", "").strip()
            or _DEFAULT_BEDROCK_MODEL,
            region=region,
        )
    raise ValueError(f"RAG_EMBEDDER={name!r} is not one of ('tfidf', 'bedrock').")


@dataclass
class RunResult:
    verdicts: list[FileVerdict]
    # path -> the name that is safe to print for it. Under manifest admission a
    # filename is graded by nothing, so it is never echoed: report, CLI and log
    # lines all read through this map (see `corpus_doc_id`).
    display_names: dict[str, str]
    n_chunks: int
    cache_hits: int
    cache_misses: int
    threshold: float
    evals: list[QueryEval]
    agg: Aggregate
    report_path: Path
    report_text: str
    embedder_signature: str
    # A provider backend runs cacheless (see cache_enabled), so cache_hits/misses
    # are both 0 no matter how much was embedded and cannot describe the run. The
    # provider counters below are what the post-run report reads in that mode;
    # `caching` says which pair is meaningful rather than leaving a reader to
    # infer it from two zeros.
    caching: bool
    provider_calls: int
    provider_retries: int
    provider_input_tokens: int


def corpus_signature(chunks: list[Chunk]) -> str:
    """A short content-address for the indexed corpus, for threshold provenance.

    Derived from each chunk id paired with the digest of its text, so it changes
    when a document is added, removed, renamed, re-sectioned, or edited in place.
    Ids alone would not: on the default slug path an id is the filename stem plus
    the section slug, so rewriting the body of a policy without touching its name
    or its headings moves the embeddings — and the threshold they calibrate —
    while leaving every id identical. Order-independent, because index order is
    not part of what the threshold was calibrated on.

    Digests, never text: this string is printed in the report, and the client's
    retention limits forbid carrying retrieved content out of a run.
    """
    joined = "\n".join(
        f"{c.chunk_id} {hashlib.sha256(c.text.encode('utf-8')).hexdigest()}"
        for c in sorted(chunks, key=lambda c: c.chunk_id)
    )
    return "corpus-" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def threshold_errors(
    answerable_tops: list[float], unanswerable_tops: list[float], threshold: float
) -> tuple[int, int]:
    """(answerable that would wrongly abstain, abstentions that are false-confident).

    Reported beside the threshold because on a corpus whose sections repeat
    across documents the two score distributions can overlap completely — the
    highest-scoring abstention case outranking every answerable one. When that
    happens no cutoff separates them, the calibrated value is already optimal,
    and the remaining errors are a property of the corpus. Without this count a
    reader sees a low abstention score and reaches for the threshold, which is
    the one thing that cannot help.
    """
    return (
        sum(1 for s in answerable_tops if s < threshold),
        sum(1 for s in unanswerable_tops if s >= threshold),
    )


def calibrate_threshold(
    answerable_tops: list[float], unanswerable_tops: list[float]
) -> float:
    """Empirical threshold (DL-6): minimum-error split over the whole cutoff space.

    Error = answerable tops below threshold (would wrongly abstain) plus
    unanswerable tops at/above it (false-confident retrieval). Ties prefer
    the widest gap. The value and method are recorded in the report.

    `errors()` is constant between adjacent observed scores, so one candidate per
    region searches the space exhaustively: the midpoints cover the interior, and
    the two outer regions need candidates of their own — the lowest score itself
    (retrieve everything) and the first float above the highest (abstain always).
    Both are reachable minima when the classes invert, which is the overlap the
    report's own prose describes: with one answerable at 0.2 under one abstention
    case at 0.3, the only midpoint costs two errors while any cutoff at or below
    0.2 costs one. Without them the report's minimum-error claim is stronger than
    the search behind it. They carry gap 0.0, so an interior candidate — which
    has real separation on both sides — wins any tie.
    """
    points = sorted(set(answerable_tops + unanswerable_tops))
    # Fewer than two distinct scores is not a calibration, and the outer regions
    # are not searched here on purpose: 0.0 is the sentinel for "too little
    # evidence to set a cutoff", and returning a real-looking value derived from
    # one observation would invite exactly the transcription this function's
    # provenance reporting exists to prevent. Pinned by test_run.py.
    if len(points) < 2:
        return 0.0
    candidates = [((a + b) / 2, b - a) for a, b in zip(points, points[1:])]
    candidates += [(points[0], 0.0), (math.nextafter(points[-1], math.inf), 0.0)]

    def errors(t: float) -> int:
        return sum(1 for s in answerable_tops if s < t) + sum(
            1 for s in unanswerable_tops if s >= t
        )

    return min(candidates, key=lambda c: (errors(c[0]), -c[1]))[0]


def run(
    base: Path = Path("."),
    gold_path: Path | None = None,
    manifest_path: Path | None = None,
    displayed_summaries_manifest_path: Path | None = None,
) -> RunResult:
    """Gate, ingest, embed, retrieve, report.

    `gold_path` names a gold set outside the repository. The client's questions
    cannot be committed — her limits forbid retaining question text, and this
    repository is a public fork — so they are read from the working directory
    that already holds her corpus. Omitted, the committed set is used and
    behaviour is unchanged.

    `manifest_path` names a checksum manifest declaring the approved POLICY corpus,
    with names relative to `base` (`policies/X.md`) — the same shape a supplied
    delivery's own `shasum -a 256` output already has, so it is usable verbatim
    rather than transcribed. Admission then comes from that declaration instead of
    the lowercase-slug convention, which is how a corpus whose filenames are not
    slugs is indexed without renaming it and invalidating its checksums.

    The manifest governs `policies/` only. `kb_dump/` keeps its own pinned
    exception (`_LEGACY_DUMP_SHA256`); a policy manifest must not turn the legacy
    dump into an unlisted-file abort.

    `displayed_summaries_manifest_path` names the checksum manifest shipped
    alongside the client's synthetic-displayed-summaries package (her own
    `shasum -a 256` output, sitting next to the files it covers). It is a
    second, unrelated frozen artifact — not part of the policy corpus and not
    fed into ingestion — verified the same way and audited first, before
    ingest or retrieval touch anything, so a support-test run never scores
    against a summaries file that drifted from what she approved.
    """

    if displayed_summaries_manifest_path is not None:
        try:
            summaries_manifest = load_corpus_manifest(displayed_summaries_manifest_path)
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"displayed-summaries manifest unusable: {exc}"
            ) from None
        summaries_problems = audit_corpus_against_manifest(
            displayed_summaries_manifest_path.parent,
            summaries_manifest,
            manifest_file=displayed_summaries_manifest_path,
        )
        if summaries_problems:
            raise RuntimeError(
                "displayed-summaries package does not match its manifest: "
                + "; ".join(summaries_problems)
            )

    # Scan EVERY file under the corpus roots, recursively and regardless of
    # extension. scan_file reads known text/JSON formats and refuses unknown or
    # non-UTF-8 ones (fail closed), so a new customers.csv or a copied text dump
    # trips the gate instead of slipping past an extension filter. With main()
    # failing closed on refusal, a contaminated file anywhere under a root breaks
    # CI. Only a narrow allowlist of benign VCS/OS metadata is skipped — a
    # dot-prefixed data file (.customers.csv) is NOT hidden from the gate.
    def _corpus_files(root: Path) -> list[Path]:
        return [
            p
            for p in root.rglob("*")
            if p.is_file() and p.name.lower() not in _SKIP_NAMES
        ]

    candidates = sorted(
        _corpus_files(base / "policies") + _corpus_files(base / "kb_dump")
    )

    manifest = None
    if manifest_path is not None:
        try:
            manifest = load_corpus_manifest(manifest_path)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"corpus manifest unusable: {exc}") from None
        problems = audit_corpus_against_manifest(base, manifest, subtree="policies")
        if problems:
            # Abort rather than index the listed subset: a directory that does not
            # match its manifest is not the corpus that was approved, and a report
            # over part of it would read as a report over all of it.
            raise RuntimeError(
                "policy corpus does not match its manifest: " + "; ".join(problems)
            )

    # A filename is committed corpus metadata — an input surface too. A file with
    # clean CONTENT but PII in its name (policies/Jane-Doe-330-90-5512.md) would
    # pass scan_file, then its path is written into the report and its stem
    # becomes the chunk id. Scan the corpus-relative path and fail closed BEFORE
    # any report/chunk/cache work, identifying offenders by position only so the
    # raw name is never echoed to logs or artifacts. unsafe_corpus_path_reason
    # covers both the PII-shape check and the lowercase-slug convention (see its
    # docstring); policy_retrieval reuses the same function at runtime.
    # The manifest covers the policy corpus only; every other root keeps the
    # naming convention.
    path_reasons = [
        unsafe_corpus_path_reason(
            p.relative_to(base),
            base=base,
            manifest=(
                manifest
                if manifest is not None
                and p.relative_to(base).parts[:1] == ("policies",)
                else None
            ),
        )
        for p in candidates
    ]
    pii_paths = [i for i, r in enumerate(path_reasons) if r == "pii"]
    if pii_paths:
        raise RuntimeError(
            f"corpus file path(s) at position(s) {pii_paths} contain PII in their "
            "names — rename them (paths are not echoed here)"
        )
    not_approved = [
        i
        for i, r in enumerate(path_reasons)
        if r in {"not-in-manifest", "manifest-digest-mismatch"}
    ]
    if not_approved:
        # Reachable even after a clean audit: the audit compares sets, this
        # re-checks each file's content at the moment it is about to be read.
        raise RuntimeError(
            f"corpus path(s) at position(s) {not_approved} are not approved by the "
            "manifest (path not echoed here)"
        )
    unsafe_names = [i for i, r in enumerate(path_reasons) if r == "non-slug"]
    if unsafe_names:
        raise RuntimeError(
            f"corpus path(s) at position(s) {unsafe_names} have a non-slug "
            "component — rename dirs/files to [a-z0-9._-] so unlabeled "
            "names/addresses cannot leak via the path (path not echoed here)"
        )

    verdicts = [scan_file(p) for p in candidates]
    cache_path = base / "rag_eval" / ".cache" / "embeddings.json"

    # Every place a corpus file is NAMED downstream — the report's hygiene table,
    # main()'s refusal lines, the chunk ids below — reads this map rather than the
    # path. Same manifest scoping as `path_reasons` above: the declaration governs
    # the policy corpus only, so every other root keeps the slug convention, whose
    # names ARE graded and stay readable.
    display_names = {}
    doc_ids = {}
    for v in verdicts:
        rel = Path(v.path).relative_to(base)
        scoped = (
            manifest
            if manifest is not None and rel.parts[:1] == ("policies",)
            else None
        )
        doc_ids[v.path] = corpus_doc_id(rel, scoped)
        # A display name is NOT a doc id. Without a manifest the slug convention
        # has graded the path, so the report keeps the readable full path while
        # the doc id stays the bare stem — the stem is what collides across
        # directories, and the duplicate-id abort below depends on it doing so.
        display_names[v.path] = (
            doc_ids[v.path] if scoped is not None else rel.as_posix()
        )

    # Gold sets name their anchor by source FILENAME, which is what the client
    # freezes; the chunker keys chunks by doc id, which under a manifest is
    # digest-derived. This is the only bridge between the two.
    doc_id_by_source: dict[str, str] = {}
    for _path, _did in doc_ids.items():
        _name = Path(_path).name
        if _name in doc_id_by_source and doc_id_by_source[_name] != _did:
            # Two admitted files share a filename in different folders. The
            # anchor would be ambiguous, so refuse rather than pick one.
            raise RuntimeError(
                "two admitted corpus files share a filename, so a gold anchor "
                "naming it would be ambiguous (name not echoed here)"
            )
        doc_id_by_source[_name] = _did

    # THE GATE (spec D2.4): only gate-passed markdown reaches the chunker.
    chunks: list[Chunk] = []
    for v in verdicts:
        if v.passed and v.path.endswith(".md"):
            chunks.extend(chunk_markdown(v.path, doc_id=doc_ids[v.path]))
    # Recursive discovery can surface two docs with the same stem in different
    # folders → same doc# id prefix. The chunker guards collisions within one
    # file; guard across files here so the gold-set id contract still holds.
    # Under a manifest the ids are digest-derived, so a collision means two
    # listed files with identical content rather than a stem clash.
    ids = [c.chunk_id for c in chunks]
    dupes = sorted({cid for cid in ids if ids.count(cid) > 1})
    if dupes:
        raise RuntimeError(
            f"duplicate chunk ids across corpus files: {dupes} — two docs share "
            "a filename stem; rename one so chunk ids stay unique"
        )
    if not chunks:
        # Nothing survives the gate, so nothing should remain cached. save() —
        # which prunes stale vectors — is never reached on this abort path, so
        # purge the prior run's cache here; otherwise PII-bearing vectors from a
        # now-removed/refused document would linger (cache.py, ADR 0007 rule 5).
        cache_path.unlink(missing_ok=True)
        refused = sum(1 for v in verdicts if not v.passed)
        raise RuntimeError(
            f"no gate-passed corpus to index under {base.resolve()} "
            f"({len(verdicts)} candidate files scanned, {refused} refused) — "
            "run from the repo root, or fix the corpus"
        )

    # Gold queries are a committed input surface too. An author could paste a
    # real officer example carrying customer PII — which would be embedded (sent
    # to the external API on the Bedrock backend) and written into the report.
    # The report prints query_id and expected as well as the query, so scan
    # EVERY string field, not just the query. Fail closed HERE, before any
    # embedder/cache side effect, and identify offenders by position only —
    # never echo a field value (the id itself could be the PII).
    # Built after chunking: a derived anchor is validated against the ids that
    # actually exist, not against the ids a gold set hoped for.
    chunk_ids = {c.chunk_id for c in chunks}
    gold_source = gold_path or GOLD_PATH
    gold = json.loads(gold_source.read_text(encoding="utf-8"))["queries"]
    # Schema-harden first: lock the structured fields to machine shapes so they
    # cannot smuggle free-text PII (a name hidden in an id or an expected entry),
    # and reject unknown keys so a future free-text field cannot appear that the
    # scan below never anticipated. Offenders by position only — never echo a
    # value, which could itself be the PII.
    for i, q in enumerate(gold):
        if not isinstance(q, dict):
            raise RuntimeError(f"gold query at position {i} is not an object")
        for alias, canonical in _GOLD_KEY_ALIASES.items():
            if alias not in q:
                continue
            if canonical in q:
                # Refuse rather than pick: a silent precedence would score the
                # row against whichever half the loader happened to prefer.
                raise RuntimeError(
                    f"gold query at position {i} carries both spellings of "
                    f"'{canonical}' — give one, not both spellings"
                )
            q[canonical] = q.pop(alias)
        extra = set(q) - _ALLOWED_GOLD_KEYS
        if extra:
            raise RuntimeError(
                f"gold query at position {i} has unknown field(s) {sorted(extra)} "
                f"— allowed keys are {sorted(_ALLOWED_GOLD_KEYS)}"
            )
        if not (isinstance(q.get("id"), str) and _GOLD_ID.fullmatch(q["id"])):
            raise RuntimeError(
                f"gold query at position {i} has a missing or non-slug id "
                "(must match [a-z0-9-])"
            )
        if not (isinstance(q.get("query"), str) and q["query"].strip()):
            raise RuntimeError(
                f"gold query at position {i} is missing a non-empty 'query' string"
            )
        expected = q.get("expected", [])
        if not (
            isinstance(expected, list)
            and all(isinstance(e, str) and _CHUNK_ID.fullmatch(e) for e in expected)
        ):
            raise RuntimeError(
                f"gold query at position {i} has an 'expected' that is not a list "
                "of chunk-id slugs (doc#section)"
            )
        src_doc = q.get("source_document")
        src_head = q.get("source_heading")
        if (src_doc is None) != (src_head is None):
            raise RuntimeError(
                f"gold query at position {i} names only one half of its frozen "
                "anchor — 'source_document' and 'source_heading' go together"
            )
        if src_doc is not None:
            if not (
                isinstance(src_doc, str)
                and src_doc.strip()
                and isinstance(src_head, str)
                and src_head.strip()
            ):
                raise RuntimeError(
                    f"gold query at position {i} has a non-string or empty "
                    "'source_document'/'source_heading'"
                )
            if expected:
                # Two sources of truth for the same fact drift apart silently,
                # and the anchor is the one the client froze.
                raise RuntimeError(
                    f"gold query at position {i} names both a frozen anchor and "
                    "a literal 'expected' — give one, and prefer the anchor"
                )
            resolved_doc = doc_id_by_source.get(src_doc)
            if resolved_doc is None:
                # Fail closed: silently scoring 0 for an anchor whose document was
                # never admitted reads as a retrieval failure, not a config error.
                raise RuntimeError(
                    f"gold query at position {i} names a 'source_document' that "
                    "is not in the admitted corpus (name not echoed here)"
                )
            expected = [f"{resolved_doc}#{_slug(src_head)}"]
            if expected[0] not in chunk_ids:
                # An anchor that resolves to no chunk would score 0 on every
                # retrieval and read as a retrieval failure. It is a typo, a
                # renamed heading, or a document that never entered the corpus —
                # all config errors, and all silent without this. The heading is
                # not echoed: it reaches the report through the chunk id.
                raise RuntimeError(
                    f"gold query at position {i} has a frozen anchor that matches "
                    "no section in the admitted corpus (anchor not echoed here)"
                )
            q["expected"] = expected
        if "unanswerable" in q and not isinstance(q["unanswerable"], bool):
            raise RuntimeError(
                f"gold query at position {i} has a non-boolean 'unanswerable'"
            )
        if "note" in q and not isinstance(q["note"], str):
            raise RuntimeError(f"gold query at position {i} has a non-string 'note'")
        topic = q.get("topic")
        if topic is not None and topic not in TOPIC_CODES and topic != UNMAPPED:
            # A code the officer channel cannot emit is a case the product cannot
            # be asked. Scoring it would report coverage that does not exist, so
            # the loader refuses rather than inventing a bucket.
            raise RuntimeError(
                f"gold query at position {i} has a 'topic' outside the officer "
                f"vocabulary — allowed values are {sorted(TOPIC_CODES)} "
                f"or {UNMAPPED!r}"
            )
        outcome_class = q.get("outcome_class")
        # isinstance first: a JSON array/object value is unhashable, and testing
        # it against the frozenset raises a raw TypeError instead of the schema
        # error every other malformed field here fails with.
        if outcome_class is not None and (
            not isinstance(outcome_class, str) or outcome_class not in _OUTCOME_CLASSES
        ):
            raise RuntimeError(
                f"gold query at position {i} has an unknown 'outcome_class' "
                f"— allowed values are {sorted(_OUTCOME_CLASSES)}"
            )
        # `no_match` and `unanswerable` state the same fact. Allowing them to
        # disagree would score an abstention case on rank of an expected chunk it
        # does not have, or score a retrieval case on staying below the
        # threshold — either way silently, and only in the class whose whole
        # purpose is proving abstention works.
        if outcome_class is not None:
            if (outcome_class == "no_match") != bool(q.get("unanswerable")):
                raise RuntimeError(
                    f"gold query at position {i} sets outcome_class and "
                    "'unanswerable' inconsistently — 'no_match' requires "
                    "unanswerable true, and every other class requires it false "
                    "or absent"
                )

        # The class says what a case is FOR; `expected` is what scores it. Left
        # free to disagree, a scored class with no expected chunk is unhittable:
        # it scores incorrect however retrieval behaves, and its empty expected
        # cell reads as the abstention case in the very table added to make a
        # class-level failure visible (report.py now labels that cell by class
        # too). Resolve the class the way QueryEval.__post_init__ does, so a
        # set that omits the field is held to the same rule.
        resolved = outcome_class or ("no_match" if q.get("unanswerable") else "answer")
        if resolved == "no_match" and expected:
            raise RuntimeError(
                f"gold query at position {i} resolves to outcome_class "
                "'no_match' but carries 'expected' chunk ids — an abstention "
                "case is scored on staying below the threshold and must leave "
                "'expected' empty"
            )
        # `clarification` is UNSCORABLE_CLASS: aggregate() drops the row from
        # every denominator, so a target here is validated, resolved and
        # printed while nothing ever scores against it — the same
        # two-sources-of-truth drift the anchor fields exist to prevent.
        if resolved == UNSCORABLE_CLASS and (expected or src_doc is not None):
            raise RuntimeError(
                f"gold query at position {i} resolves to outcome_class "
                f"'{UNSCORABLE_CLASS}' but carries 'expected' chunk ids or a "
                "frozen anchor — an unscorable case is excluded from every "
                "denominator and must leave both empty"
            )
        if resolved not in ("no_match", UNSCORABLE_CLASS) and not expected:
            raise RuntimeError(
                f"gold query at position {i} resolves to outcome_class "
                f"'{resolved}' but names no 'expected' chunk ids — every class "
                "except 'no_match' is scored on retrieval rank and must name at "
                "least one expected chunk"
            )

    # Then screen the remaining free text (query/note) for self-identifying PII.
    dirty = [
        i for i, q in enumerate(gold) if any(scan_text(s) for s in _gold_strings(q))
    ]
    if dirty:
        raise RuntimeError(
            f"gold queries at position(s) {dirty} contain PII and must be "
            f"sanitized ({gold_source}) — no field values are echoed"
        )
    # scan_text is label-gated for names; also refuse a probable person name in
    # free text, which would otherwise be embedded (Bedrock) and printed to the
    # report. Position-only — the name itself is the PII, so it is never echoed.
    # Structured anchor fields are excluded (see _ANCHOR_KEYS): an ordinary
    # policy heading is title-case ("Adverse Action"), which the heuristic reads
    # as a probable person name, and a resolved anchor is already constrained to
    # a section of the admitted corpus.
    named = [
        i
        for i, q in enumerate(gold)
        if any(
            _looks_like_person_name(s)
            for s in _gold_strings(
                {k: v for k, v in q.items() if k not in _ANCHOR_KEYS}
            )
        )
    ]
    if named:
        raise RuntimeError(
            f"gold queries at position(s) {named} contain a probable person name "
            f"and must use synthetic ids/placeholders ({gold_source}) "
            "— no field values are echoed"
        )

    embedder = make_embedder()
    # Both decisions read the embedder itself, so they cannot disagree with the
    # backend that actually makes the calls.
    refuse_traced_provider_run(embedder)
    embedder.fit([c.text for c in chunks])
    cache = EmbeddingCache(cache_path)
    caching = cache_enabled(embedder)
    if not caching:
        # A provider backend runs cacheless: the vectors are content-derived and
        # the client excluded retention. Purge anything a prior TF-IDF run left,
        # so "no retention" is a state on disk rather than a claim about this run.
        cache_path.unlink(missing_ok=True)
    index = InMemoryIndex()
    try:
        for c in chunks:
            index.add(
                c.chunk_id,
                cache.get_or_embed(embedder.signature, c.text, embedder.embed)
                if caching
                else embedder.embed(c.text),
            )
        if caching:
            cache.save()
    except Exception:
        # A partial/failed embed run (e.g. a Bedrock timeout mid-loop) must not
        # leave the prior cache — which may hold vectors for a now-removed or
        # newly refused source — intact on disk. Purge it so a retry rebuilds
        # cleanly rather than serving stale PII-bearing vectors (ADR 0007 rule 5).
        cache_path.unlink(missing_ok=True)
        raise

    retrieved = {q["id"]: index.search(embedder.embed(q["query"]), k=5) for q in gold}

    def tops(unanswerable: bool) -> list[float]:
        # Unscorable cases leave calibration for the same reason aggregate()
        # drops them: they have no scoring target, so admitting one as an
        # answerable example moves the threshold every scored case is graded
        # against. Same predicate as QueryEval.scorable.
        return [
            retrieved[q["id"]][0][1] if retrieved[q["id"]] else 0.0
            for q in gold
            if q.get("outcome_class") != UNSCORABLE_CLASS
            and bool(q.get("unanswerable")) == unanswerable
        ]

    threshold = calibrate_threshold(tops(False), tops(True))
    evals = [
        QueryEval(
            query_id=q["id"],
            query=q["query"],
            expected=q.get("expected", []),
            unanswerable=bool(q.get("unanswerable")),
            retrieved=retrieved[q["id"]],
            threshold=threshold,
            topic=q.get("topic", UNMAPPED),
            outcome_class=q.get("outcome_class"),
        )
        for q in gold
    ]

    agg = aggregate(evals)
    # Read the counters AFTER the gold-query embeds above: a provider run embeds
    # every indexed chunk and every gold query, and both are provider calls the
    # client's report has to account for. TF-IDF carries no such counters.
    provider_calls = getattr(embedder, "calls", 0)
    provider_retries = getattr(embedder, "retries", 0)
    provider_input_tokens = getattr(embedder, "input_tokens", 0)
    _ans_tops = [
        e.retrieved[0][1]
        for e in evals
        if e.scorable and not e.unanswerable and e.retrieved
    ]
    _una_tops = [
        e.retrieved[0][1]
        for e in evals
        if e.scorable and e.unanswerable and e.retrieved
    ]
    _wrong_abstain, _false_confident = threshold_errors(_ans_tops, _una_tops, threshold)
    report_text = report_mod.build(
        verdicts=verdicts,
        display_names=display_names,
        n_chunks=len(chunks),
        cache_hits=cache.hits,
        cache_misses=cache.misses,
        caching=caching,
        provider_calls=provider_calls,
        provider_retries=provider_retries,
        provider_input_tokens=provider_input_tokens,
        threshold=threshold,
        evals=evals,
        agg=agg,
        embedder_signature=embedder.signature,
        corpus_signature=corpus_signature(chunks),
        wrong_abstain=_wrong_abstain,
        false_confident=_false_confident,
    )
    report_path = base / "rag_eval" / "eval_report.md"
    # Created explicitly: this directory used to exist only as a side effect of
    # EmbeddingCache.save() writing its parent, so a cacheless provider run —
    # which is the graded configuration — reached this line with no directory.
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text, encoding="utf-8")
    return RunResult(
        verdicts=verdicts,
        display_names=display_names,
        n_chunks=len(chunks),
        cache_hits=cache.hits,
        cache_misses=cache.misses,
        threshold=threshold,
        evals=evals,
        agg=agg,
        report_path=report_path,
        report_text=report_text,
        embedder_signature=embedder.signature,
        caching=caching,
        provider_calls=provider_calls,
        provider_retries=provider_retries,
        provider_input_tokens=provider_input_tokens,
    )


def main(
    base: Path = Path("."),
    gold_path: Path | None = None,
    manifest_path: Path | None = None,
    displayed_summaries_manifest_path: Path | None = None,
) -> None:
    result = run(
        base=base,
        gold_path=gold_path,
        manifest_path=manifest_path,
        displayed_summaries_manifest_path=displayed_summaries_manifest_path,
    )
    refused = [v for v in result.verdicts if not v.passed]
    print(f"gate: {len(result.verdicts)} files scanned, {len(refused)} refused")
    for v in refused:
        # Named through display_names, never the raw path: a manifest-admitted
        # filename is graded by nothing and can itself be the identifier.
        print(f"  REFUSED {result.display_names.get(v.path, v.path)}: {v.counts()}")
    print(f"embedder: {result.embedder_signature}")
    if result.caching:
        print(
            f"embeddings: {result.n_chunks} chunks, "
            f"{result.cache_misses} embedded this run, {result.cache_hits} from cache"
        )
    else:
        # Cacheless provider run: the cache counters are structurally 0 here, so
        # printing them would report a no-op for a run that embedded everything.
        print(
            f"embeddings: {result.n_chunks} chunks, {result.provider_calls} provider "
            f"calls this run ({result.provider_retries} retries, "
            f"{result.provider_input_tokens} input tokens), cache disabled"
        )
    print(f"threshold: {result.threshold!r} (calibrated, see report)")
    print(f"report: {result.report_path}")

    # Fail closed: the report is written above (so the refusal is always
    # diagnosable), but a refusal of anything other than the known legacy dump
    # is a new PII-in-repo regression and must break the CI rag-eval-gate.
    unexpected = [v for v in refused if not _refusal_is_expected(v.path, base)]
    if unexpected:
        print(
            "FAIL: hygiene gate refused non-legacy corpus file(s) — "
            "new PII committed to the repo:"
        )
        for v in unexpected:
            print(f"  {result.display_names.get(v.path, v.path)}: {v.counts()}")
        raise SystemExit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Retrieval eval harness (gate -> ingest -> embed -> report)."
    )
    parser.add_argument(
        "--base",
        type=Path,
        default=Path("."),
        help="working directory holding policies/ and receiving rag_eval/ outputs",
    )
    parser.add_argument(
        "--gold",
        type=Path,
        default=None,
        help=(
            "gold query set to score, in the committed set's schema. Defaults to "
            "rag_eval/gold_queries.json. Use it to score a question set that "
            "must not be committed."
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help=(
            "checksum manifest (shasum -a 256) declaring the approved policy "
            "corpus, names relative to --base (policies/X.md). Without it the "
            "lowercase-slug naming convention governs admission."
        ),
    )
    parser.add_argument(
        "--displayed-summaries-manifest",
        type=Path,
        default=None,
        help=(
            "checksum manifest (shasum -a 256) for the synthetic-displayed-"
            "summaries package, sitting next to the files it covers. Audited "
            "before ingest or retrieval; unrelated to --manifest."
        ),
    )
    args = parser.parse_args()
    main(
        base=args.base,
        gold_path=args.gold,
        manifest_path=args.manifest,
        displayed_summaries_manifest_path=args.displayed_summaries_manifest,
    )
