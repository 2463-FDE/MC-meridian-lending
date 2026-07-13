# Scaling the RAG eval harness: Bedrock embeddings → pgvector

**Status:** Phase 1 implemented (branch `feature/rag-eval-impl-week2`). Phases 2–3 scoped, not built.
**Date:** 2026-07-13
**Related:** `rag_eval/README.md`, ADR 0007 (corpus hygiene), ADR 0005 (LLM client), `docs/spec-rag-week2.md`

## Why this doc exists

The Week 2 harness ships with a pure-Python TF-IDF embedder and an in-memory
cosine index — deliberately minimal for a 9-chunk corpus (ADR 0007 rule 6). The
question was: *how do we scale, given Amazon Bedrock is the intended path?*

Answer: phase it against real triggers, don't big-bang it. The `fit/embed/signature`
embedder contract and the `add/search/__len__` index contract are the seams the
whole design was built around — scaling is swapping an implementation behind a
contract that already exists, not a rewrite. **Do not build ahead of the trigger**
(CLAUDE.md YAGNI): a persistent vector DB for 9 chunks is pure carrying cost.

## The two swaps are independent

The original ask ("Bedrock + pgvector") bundles two changes that scale on
different triggers and carry very different cost/risk:

| Swap | Trigger | Risk | Reopens ADR? |
|------|---------|------|--------------|
| Embedding backend (TF-IDF → Bedrock) | quality/semantics need dense vectors | low — fits existing contract | no |
| Index (in-memory → pgvector) | corpus size (~hundreds+ chunks) | higher — persistent store, docker, schema | **yes, ADR 0007 rule 6** |

Phase 1 does the first. The second is Phase 2, deferred until the corpus actually
grows — at 9 chunks brute-force exact cosine is microseconds and pgvector buys
nothing.

---

## Phase 1 — Bedrock embeddings (DONE)

Real Bedrock embeddings selectable at runtime, TF-IDF still the default so CI stays
keyless/offline. Index stays in-memory.

### What changed
- **`embedder.py`** — `cosine()` now dispatches on vector shape (dense `list` vs
  sparse `dict`); added `BedrockEmbedder` (lazy `boto3`, `fit()` no-op that binds
  the signature to the model id, `embed()` returns an L2-normalized `list[float]`).
- **`index.py`, `cache.py`** — type hints loosened to carry either vector shape.
  No logic change. Cache keys already include the embedder signature, so TF-IDF and
  Bedrock entries never collide in one cache file.
- **`run.py`** — `make_embedder()` reads `RAG_EMBEDDER` (`tfidf` default | `bedrock`);
  unknown value fails loud. Embedder signature threaded into `RunResult`, the CLI
  summary, and the report.
- **`report.py`** — run summary + threshold section name the backend signature.
- **`requirements-bedrock.txt`** — `boto3`, optional, **not** in CI.
- **Tests** — dense-cosine units; `BedrockEmbedder` with an injected fake client
  (no real API); full `run()` over a dense backend; factory default/reject. 68 pass.

### Design decisions
- **Auth mirrors ADR 0005.** Bedrock uses AWS credentials resolved by boto3
  (env/profile/role), never an API-key literal. `AWS_REGION` optional.
- **boto3 is lazy.** Imported only when `RAG_EMBEDDER=bedrock` is actually chosen,
  so the default path and the whole test suite stay stdlib-only.
- **Threshold auto-recalibrates.** Dense scores distribute differently than
  lexical ones; `calibrate_threshold()` already runs per-run off the gold set and
  records the value + backend in the report. No manual step.
- **No base class / Protocol.** Two duck-typed implementations behind a factory is
  thin enough (CLAUDE.md: no interface until the 3rd caller).
- **Hygiene gate is unchanged and still upstream of embedding** — `kb_dump` is
  refused before any vector is computed, whatever the backend (ADR 0007 rule 4).

### Run it with a real key
```bash
pip install -r rag_eval/requirements-bedrock.txt
export RAG_EMBEDDER=bedrock AWS_REGION=us-east-1     # your enabled region
# AWS creds via env/profile/role. Optionally RAG_BEDROCK_MODEL=<id>
python3 -m rag_eval.run
```
Default model: `amazon.titan-embed-text-v2:0` (1024-dim). Confirm the id is enabled
in your account/region — Bedrock model ids are region/account-specific.

### One-time smoke checklist (needs AWS creds — not yet run)
- [ ] Gate still refuses `kb_dump/applications.jsonl`.
- [ ] Embeddings hit Bedrock once, then serve from cache on a second run.
- [ ] hit@3 stays ≈ 1.0 on the gold set (or investigate if it drops).
- [ ] Report shows `bedrock-v1:<model>` and a recalibrated threshold.
- [ ] Record the numbers here for comparison against the TF-IDF baseline.

---

## Phase 2 — pgvector index (SCOPED, NOT BUILT)

Trigger: the corpus grows past roughly a few hundred chunks, or persistence across
runs becomes worth the operational cost. Until then, in-memory wins.

This is a genuine architecture change, not a swap:
- **Amend ADR 0007 rule 6** ("harness keeps no persistent chunk store") with a PII
  re-review of what lands in the store — the ADR treats embedded text as
  recoverable by default.
- **Docker:** swap `postgres:16-alpine` → `pgvector/pgvector:pg16`; add a
  `CREATE EXTENSION vector` migration and a chunk/embedding table (`db/migrations/`).
- **New `PgVectorIndex`** implementing the existing `add/search/__len__` contract —
  callers in `run.py` stay untouched (the point of the seam).
- **Wire a DB connection into the harness**, which today has zero DB deps (the
  README's "no DB, no docker-compose entry" stops being true).

## Phase 3 — production RAG service (NOT SCOPED HERE)

The harness graduates from an offline eval tool to a live service only if/when the
"AI underwriting assistant" is greenlit (`docs/STAGE1-PLAN-AI-ASSISTANT.md`). Out of
scope until that decision.

## Explicitly NOT doing now
- Building pgvector/docker/schema "to be ready" — the `Index` contract *is* the
  readiness. Building the DB layer for 9 chunks is speculative debt.
- Provider fallback logic — the `RAG_EMBEDDER` toggle is explicit; an absent key
  errors loud (same posture as the LLM client config).
