# Internal ask — 2026-08-25, Titan embeddings now, vector store later

**Audience:** engineering lead / program mentor · **Sent:** not sent · **Answered:** —

**Status: DRAFT, NOT SENT — and mostly our own work rather than an ask.** Filed here beside the
client asks because it is the other half of the same decision, not because it needs a client.
Items 1–3 below are the only parts that need someone else's yes; everything in "Titan switch"
is ours to do.

## Correction on record

An earlier note in this session said the Bedrock embedding backend was unpushed and had to merge
first. **Wrong — it is on `main`**, commit `1a985fa`, `rag_eval/embedder.py`. That came from a
stale memory entry (`rag-eval-bedrock-backend`, "4 commits unpushed"), which should be corrected.
Nothing needs merging before the Titan switch.

## Titan switch — required, no approval needed, ours to do

Titan is mandated, so `RAG_EMBEDDER=bedrock` becomes the product configuration.

| # | Item | State |
|---|------|-------|
| 1 | `RAG_EMBEDDER` + `RAG_BEDROCK_MODEL` into the origination env block | **Missing.** Not in `docker-compose.yml`; `make_embedder()` reads `os.getenv("RAG_EMBEDDER", "tfidf")` and compose passes nothing, so the running stack can only ever get TF-IDF |
| 2 | AWS credentials reaching origination-service | **Already plumbed** — `docker-compose.yml` passes `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_BEARER_TOKEN_BEDROCK` for the LLM Bedrock provider. Reuse, add nothing |
| 3 | `boto3` | Present transitively via `anthropic[bedrock]==0.116.0`. **Pin it explicitly** — a money-adjacent path should not depend on another package's extra |
| 4 | Re-calibrate `POLICY_RETRIEVAL_MIN_SCORE` | **Required.** `0.1609` was calibrated for TF-IDF; `.env.example` says so in as many words, on the `POLICY_RETRIEVAL_MIN_SCORE` line itself. Re-run `python3 -m rag_eval.run` and use what it prints |
| 5 | CI | **No change.** `BedrockEmbedder` accepts an injected client and tests leave `RAG_EMBEDDER` unset, so the suite stays keyless. Do not flip the default |
| 6 | ADR | The embedder was recorded as a toggle with a TF-IDF default; a mandate is a decision record. **Next free number is 0021** — confirm against open PRs before taking it |

**Two consequences of the switch, worth stating before anyone is surprised by them.**

- **Cold start is now permanent.** `policy_retrieval._build_index()` does not use `EmbeddingCache`
  — that is `rag_eval/run.py` only. Every process re-embeds the whole corpus on the officer's
  first search, in the request path. Nine chunks is seconds. It does not stay seconds, and it
  repeats on every restart.
- **No credentials means total abstention.** Retrieval fails closed, so a demo machine without AWS
  access answers "no policy match" to every policy question. That is correct behaviour and a
  terrible demo.

## Approvals needed before any pgvector work could start

Not proposing we build it. D16 carries three triggers and none has fired — nine chunks, one
caller, no persistence requirement — and the `Index` contract (`add`/`search`/`__len__`) is what
makes the swap cheap, so waiting costs nothing. These three want a yes in principle so the work
is not blocked at the point where measurements say go.

| # | Approval | Why it is not a detail |
|---|----------|------------------------|
| 1 | `psycopg2` into `rag_eval` | The harness has zero DB dependencies today and runs in CI with no services and no credentials. That property ends permanently |
| 2 | `postgres:16-alpine` → `pgvector/pgvector:pg16` | Same major, existing `pgdata` volume carries over, no dump/restore. But this is the one database all seven services share (ADR 0002) — one retrieval feature changing the estate's base image should be a decision, not a side effect |
| 3 | Amend ADR 0007 rule 6 | Rule 6 states the harness keeps no persistent chunk store. A vector store puts corpus prose in the shared database permanently, and the ADR treats embedded text as recoverable — so this needs a real privacy review and a purge path, mirroring the `cache_path.unlink` purge in `rag_eval/run.py` |

**Correction, 2026-08-30 — this section previously said no job in `.github/workflows/ci.yml`
runs a Postgres service, and attributed that to `migration-numbering-gate`'s own comment. Both
halves are wrong.** Two jobs run one: `assistant-telemetry-gate` and `no-sad-gate`, each on
`postgres:16`. `migration-numbering-gate` says nothing about Postgres; the claim came from a
stale comment in `ci.yml` that has since been corrected on `main`. What still holds is the
consequence: neither existing service carries pgvector, so testing it properly means changing an
image rather than adding a service, and faking it leaves migration SQL unexecuted — which is
exactly how a migration that could not parse merged earlier this month.

**Numbers that argue against building early:** 1024 dims × 4 bytes = 4KB per chunk; 1,000 chunks
is about 4MB. Do **not** create an HNSW or IVFFlat index — under roughly 10k rows a sequential
scan beats the index, and building one is the same speculative cost as building the store itself.

## Proposed order

1. Switch to Titan (the table above). Ours, this week.
2. Get the corpus and the officer questions from the client (`docs/client-asks/2026-08-25-policy-corpus.md`).
3. Measure. Rebuild the gold set, re-derive the threshold.
4. If cold-start cost is what hurts, reuse `EmbeddingCache` on the product path first — already
   written, already privacy-aware, no schema, no extension, no shared-database change.
5. Only if that is insufficient, or a second service needs the same vectors, does pgvector open as
   its own PR with the ADR amendment attached.

## Draft email — not sent

_The email body for this section is held outside this repository with the other client-facing copies. This log keeps the reasoning, the decisions and her replies; the sent text is not published here._
