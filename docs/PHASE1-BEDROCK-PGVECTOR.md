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
- **Auth mirrors ADR 0005.** Bedrock creds are resolved by boto3, never an
  API-key literal in code. Any boto3-supported method works: an IAM
  access-key/profile/role, **or** a Bedrock API key (bearer token) via
  `AWS_BEARER_TOKEN_BEDROCK` (what the smoke used). `run.py` only ever reads
  `AWS_REGION`/`RAG_BEDROCK_MODEL`; the credential is boto3's concern.
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
pip install -U -r rag_eval/requirements-bedrock.txt   # -U: bearer-token support needs botocore >= 1.35
export RAG_EMBEDDER=bedrock AWS_REGION=us-east-1       # your enabled region
# Credential — either an IAM access-key/profile, or a Bedrock API key:
#   read -rs AWS_BEARER_TOKEN_BEDROCK && export AWS_BEARER_TOKEN_BEDROCK
# (read -s keeps the token off screen and out of shell history; a real TTY is
#  required, so run in a terminal, not a non-interactive `!` shell.)
# Optionally RAG_BEDROCK_MODEL=<id>
python3 -m rag_eval.run
```
Default model: `amazon.titan-embed-text-v2:0` (1024-dim). Confirm the id is enabled
in your account/region — Bedrock model ids are region/account-specific.

### One-time smoke — PASS (2026-07-13, `amazon.titan-embed-text-v2:0`)
- [x] Gate still refuses `kb_dump/applications.jsonl` (ssn/pan/dob ×5, ein ×1), masked samples.
- [x] Embeddings hit Bedrock once (9 embedded this run, 0 from cache); second run serves all from cache.
- [x] hit@3 held at 1.00 on the gold set.
- [x] Report shows `bedrock-v1:amazon.titan-embed-text-v2:0` and a recalibrated threshold (0.2397, was 0.1806 for TF-IDF).

**Results vs the TF-IDF baseline** (9-chunk corpus, 10 answerable + 2 unanswerable):

| Metric | TF-IDF (default) | Bedrock Titan v2 |
|--------|------------------|------------------|
| hit@1  | 0.90 | 0.70 |
| hit@3  | 1.00 | 1.00 |
| hit@5  | 1.00 | 1.00 |
| MRR    | 0.95 | 0.85 |
| Unanswerable correct | 1/2 (#6012 false-confident) | **2/2** |
| Calibrated threshold | 0.1806 | 0.2397 |

Read: at 9 chunks TF-IDF wins on top-1 (lexical overlap is strong on keyword-dense
policy text), but both put every answerable query in the top 3. Titan's semantic
separation **fixed the #6012 trap** — the adverse-action chunk TF-IDF retrieved with
false confidence now scores below threshold, so both unanswerable queries correctly
abstain. Conclusion holds: TF-IDF stays the default at this corpus size; Titan's edge
appears as the corpus grows past lexical overlap — the Phase 2 trigger.

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
