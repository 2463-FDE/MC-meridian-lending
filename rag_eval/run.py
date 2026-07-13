"""Eval harness runner: gate -> ingest -> embed (cached) -> retrieve -> report.

One command, zero LLM calls (spec D1.1): ``python -m rag_eval.run``.

The hygiene gate is a hard precondition enforced here in code (spec D2.4,
ADR 0007 rule 4): chunks are only ever produced from gate-passed files inside
``run()`` — there is no other path into the embedder, and no override flag.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from rag_eval import report as report_mod
from rag_eval.cache import EmbeddingCache
from rag_eval.chunker import Chunk, chunk_markdown
from rag_eval.embedder import BedrockEmbedder, TfidfEmbedder
from rag_eval.hygiene import FileVerdict, scan_file
from rag_eval.index import InMemoryIndex
from rag_eval.metrics import Aggregate, QueryEval, aggregate

GOLD_PATH = Path(__file__).parent / "gold_queries.json"

# The ONE corpus file ADR 0007 documents as legacy-contaminated: kb_dump is the
# raw pre-remediation dump, so its refusal is expected and is the whole point of
# the hygiene report. EVERY other refusal — a policy doc, or any new file — is a
# fresh PII-in-repo regression and must fail the CI gate (enforced in main()).
_EXPECTED_CONTAMINATED = ("kb_dump", "applications.jsonl")


def _refusal_is_expected(path_str: str) -> bool:
    p = Path(path_str)
    return (p.parent.name, p.name) == _EXPECTED_CONTAMINATED


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
    # Scan the corpus roots RECURSIVELY: a markdown doc dropped in a policies/
    # subdirectory, or any .jsonl added under kb_dump/, must not slip past the
    # gate unscanned. With main() failing closed on refusal, a wider scan means
    # a contaminated file anywhere under a root trips the gate, not just the two
    # original top-level paths. (Only these two known roots/extensions — not a
    # generic all-types scan; there is no other corpus artifact shape yet.)
    policy_files = sorted((base / "policies").rglob("*.md"))
    kb_files = sorted((base / "kb_dump").rglob("*.jsonl"))
    candidates = policy_files + kb_files
    verdicts = [scan_file(p) for p in candidates]

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
        refused = sum(1 for v in verdicts if not v.passed)
        raise RuntimeError(
            f"no gate-passed corpus to index under {base.resolve()} "
            f"({len(verdicts)} candidate files scanned, {refused} refused) — "
            "run from the repo root, or fix the corpus"
        )

    embedder = make_embedder()
    embedder.fit([c.text for c in chunks])
    cache = EmbeddingCache(base / "rag_eval" / ".cache" / "embeddings.json")
    index = InMemoryIndex()
    for c in chunks:
        index.add(
            c.chunk_id, cache.get_or_embed(embedder.signature, c.text, embedder.embed)
        )
    cache.save()

    gold = json.loads(GOLD_PATH.read_text(encoding="utf-8"))["queries"]
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
    unexpected = [v for v in refused if not _refusal_is_expected(v.path)]
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
