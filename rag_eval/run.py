"""Eval harness runner: gate -> ingest -> embed (cached) -> retrieve -> report.

One command, zero LLM calls (spec D1.1): ``python -m rag_eval.run``.

The hygiene gate is a hard precondition enforced here in code (spec D2.4,
ADR 0007 rule 4): chunks are only ever produced from gate-passed files inside
``run()`` — there is no other path into the embedder, and no override flag.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from rag_eval import report as report_mod
from rag_eval.cache import EmbeddingCache
from rag_eval.chunker import Chunk, chunk_markdown
from rag_eval.embedder import BedrockEmbedder, TfidfEmbedder
from rag_eval.hygiene import FileVerdict, scan_file, scan_text
from rag_eval.index import InMemoryIndex
from rag_eval.metrics import Aggregate, QueryEval, aggregate

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

# Gold-query STRUCTURED fields are locked to machine shapes so they cannot carry
# free-text PII: an id is a slug, an expected entry is a chunk id (doc#section,
# from chunker.py). That leaves only the natural-language query/note as free
# text — screened by scan_text for self-identifying PII (SSN/PAN/email) and, per
# the gold_queries.json contract, required to describe synthetic scenarios only.
# (An unlabeled person name in free query text cannot be caught by regex without
# also rejecting legitimate regulatory phrases like "Fair Credit Reporting Act",
# so it stays the author's responsibility — same residual as filename slugs.)
_GOLD_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
_CHUNK_ID = re.compile(r"[a-z0-9][a-z0-9._-]*#[a-z0-9._-]+")
_ALLOWED_GOLD_KEYS = {"id", "query", "expected", "unanswerable", "note"}

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


# Titan Embed Text v2 — AWS-native, cheap, 1024-dim. Confirm the id is enabled
# in your account/region before relying on it (Bedrock model ids are
# region/account-specific), same caveat as the LLM client's Bedrock model.
_DEFAULT_BEDROCK_MODEL = "amazon.titan-embed-text-v2:0"


def make_embedder():
    """Pick the embedding backend from ``RAG_EMBEDDER`` (default ``tfidf``).

    ``tfidf`` (default) keeps CI keyless and stdlib-only. ``bedrock`` uses
    Amazon Bedrock via boto3 (``RAG_BEDROCK_MODEL``, ``AWS_REGION``, AWS creds)
    — the scaling path. An unknown value fails loud rather than silently
    falling back to a different backend than asked for.
    """
    name = os.getenv("RAG_EMBEDDER", "tfidf")
    if name == "tfidf":
        return TfidfEmbedder()
    if name == "bedrock":
        return BedrockEmbedder(
            model_id=os.getenv("RAG_BEDROCK_MODEL", _DEFAULT_BEDROCK_MODEL),
            region=os.getenv("AWS_REGION"),
        )
    raise ValueError(f"RAG_EMBEDDER={name!r} is not one of ('tfidf', 'bedrock').")


@dataclass
class RunResult:
    verdicts: list[FileVerdict]
    n_chunks: int
    cache_hits: int
    cache_misses: int
    threshold: float
    evals: list[QueryEval]
    agg: Aggregate
    report_path: Path
    report_text: str
    embedder_signature: str


def calibrate_threshold(
    answerable_tops: list[float], unanswerable_tops: list[float]
) -> float:
    """Empirical threshold (DL-6): midpoint split minimizing gold-set errors.

    Candidate thresholds are midpoints between adjacent distinct top scores.
    Error = answerable tops below threshold (would wrongly abstain) plus
    unanswerable tops at/above it (false-confident retrieval). Ties prefer
    the widest gap. The value and method are recorded in the report.
    """
    points = sorted(set(answerable_tops + unanswerable_tops))
    if len(points) < 2:
        return 0.0
    candidates = [((a + b) / 2, b - a) for a, b in zip(points, points[1:])]

    def errors(t: float) -> int:
        return sum(1 for s in answerable_tops if s < t) + sum(
            1 for s in unanswerable_tops if s >= t
        )

    return min(candidates, key=lambda c: (errors(c[0]), -c[1]))[0]


def run(base: Path = Path(".")) -> RunResult:
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

    # A filename is committed corpus metadata — an input surface too. A file with
    # clean CONTENT but PII in its name (policies/Jane-Doe-330-90-5512.md) would
    # pass scan_file, then its path is written into the report and its stem
    # becomes the chunk id. Scan the corpus-relative path and fail closed BEFORE
    # any report/chunk/cache work, identifying offenders by position only so the
    # raw name is never echoed to logs or artifacts.
    pii_paths = [
        i for i, p in enumerate(candidates) if scan_text(str(p.relative_to(base)))
    ]
    if pii_paths:
        raise RuntimeError(
            f"corpus file path(s) at position(s) {pii_paths} contain PII in their "
            "names — rename them (paths are not echoed here)"
        )

    # scan_text above only catches self-identifying PII in a path (SSN, PAN,
    # email). It CANNOT catch an unlabeled person name or street address —
    # "Jane-Doe.md" is shape-identical to a policy title "Fee-Schedule.md", so a
    # name-shape detector would refuse the whole policies/ tree. Instead require
    # every corpus path COMPONENT (each parent directory AND the filename) to be
    # a lowercase slug: a real name/address committed to the tree is
    # conventionally Title-Cased or spaced ("Jane-Doe", "123 Main St"), so
    # anything with an uppercase letter, space, or other unsafe char is refused
    # before its stem becomes a chunk id / its path enters the report. Checking
    # every component (not just p.name) closes the directory bypass — the report
    # emits the full path, so a "policies/Jane-Doe/fees.md" dir would leak too.
    # (A deliberately all-lowercase name is out of scope here; file CONTENT is
    # still fully scanned by scan_file. A leading dot is allowed so hidden data
    # files still reach the content scan rather than being refused on name.)
    unsafe_names = [
        i
        for i, p in enumerate(candidates)
        if not all(_SAFE_FILENAME.fullmatch(part) for part in p.relative_to(base).parts)
    ]
    if unsafe_names:
        raise RuntimeError(
            f"corpus path(s) at position(s) {unsafe_names} have a non-slug "
            "component — rename dirs/files to [a-z0-9._-] so unlabeled "
            "names/addresses cannot leak via the path (path not echoed here)"
        )

    verdicts = [scan_file(p) for p in candidates]
    cache_path = base / "rag_eval" / ".cache" / "embeddings.json"

    # THE GATE (spec D2.4): only gate-passed markdown reaches the chunker.
    chunks: list[Chunk] = []
    for v in verdicts:
        if v.passed and v.path.endswith(".md"):
            chunks.extend(chunk_markdown(v.path))
    # Recursive discovery can surface two docs with the same stem in different
    # folders → same doc# id prefix. The chunker guards collisions within one
    # file; guard across files here so the gold-set id contract still holds.
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
    gold = json.loads(GOLD_PATH.read_text(encoding="utf-8"))["queries"]
    # Schema-harden first: lock the structured fields to machine shapes so they
    # cannot smuggle free-text PII (a name hidden in an id or an expected entry),
    # and reject unknown keys so a future free-text field cannot appear that the
    # scan below never anticipated. Offenders by position only — never echo a
    # value, which could itself be the PII.
    for i, q in enumerate(gold):
        if not isinstance(q, dict):
            raise RuntimeError(f"gold query at position {i} is not an object")
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
        if "unanswerable" in q and not isinstance(q["unanswerable"], bool):
            raise RuntimeError(
                f"gold query at position {i} has a non-boolean 'unanswerable'"
            )
        if "note" in q and not isinstance(q["note"], str):
            raise RuntimeError(f"gold query at position {i} has a non-string 'note'")

    # Then screen the remaining free text (query/note) for self-identifying PII.
    dirty = [
        i for i, q in enumerate(gold) if any(scan_text(s) for s in _gold_strings(q))
    ]
    if dirty:
        raise RuntimeError(
            f"gold queries at position(s) {dirty} contain PII and must be "
            "sanitized (rag_eval/gold_queries.json) — no field values are echoed"
        )

    embedder = make_embedder()
    embedder.fit([c.text for c in chunks])
    cache = EmbeddingCache(cache_path)
    index = InMemoryIndex()
    try:
        for c in chunks:
            index.add(
                c.chunk_id,
                cache.get_or_embed(embedder.signature, c.text, embedder.embed),
            )
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
        return [
            retrieved[q["id"]][0][1] if retrieved[q["id"]] else 0.0
            for q in gold
            if bool(q.get("unanswerable")) == unanswerable
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
        )
        for q in gold
    ]

    agg = aggregate(evals)
    report_text = report_mod.build(
        verdicts=verdicts,
        n_chunks=len(chunks),
        cache_hits=cache.hits,
        cache_misses=cache.misses,
        threshold=threshold,
        evals=evals,
        agg=agg,
        embedder_signature=embedder.signature,
    )
    report_path = base / "rag_eval" / "eval_report.md"
    report_path.write_text(report_text, encoding="utf-8")
    return RunResult(
        verdicts=verdicts,
        n_chunks=len(chunks),
        cache_hits=cache.hits,
        cache_misses=cache.misses,
        threshold=threshold,
        evals=evals,
        agg=agg,
        report_path=report_path,
        report_text=report_text,
        embedder_signature=embedder.signature,
    )


def main(base: Path = Path(".")) -> None:
    result = run(base=base)
    refused = [v for v in result.verdicts if not v.passed]
    print(f"gate: {len(result.verdicts)} files scanned, {len(refused)} refused")
    for v in refused:
        print(f"  REFUSED {v.path}: {v.counts()}")
    print(f"embedder: {result.embedder_signature}")
    print(
        f"embeddings: {result.n_chunks} chunks, "
        f"{result.cache_misses} embedded this run, {result.cache_hits} from cache"
    )
    print(f"threshold: {result.threshold:.4f} (calibrated, see report)")
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
            print(f"  {v.path}: {v.counts()}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
