# rag_eval — Retrieval Eval Harness (Week 2)

Offline harness that measures what the lending-policy corpus can and cannot answer,
and gates what is allowed to enter a retrieval corpus. **Zero LLM calls, zero
third-party runtime deps** (stdlib only; `pytest` for tests). This is an analysis
tool, not a service — no FastAPI, no DB, no docker-compose entry.

Spec: `docs/spec-rag-week2.md` · Plan: `docs/STAGE1-PLAN-RAG-WEEK2.md` ·
ADR 0007 (corpus hygiene) · ADR 0008 (retrievable decision records).

## Run

From the repo root:

```bash
python3 -m rag_eval.run
```

Writes `rag_eval/eval_report.md` (gitignored, regenerated per run) and caches
embeddings in `rag_eval/.cache/` — a second run over an unchanged corpus
re-embeds nothing (the run summary proves it).

## What one run does

1. **Hygiene gate** (`hygiene.py`) — scans every candidate file (`policies/*.md`,
   `kb_dump/applications.jsonl`) for PII: regex + Luhn for PAN, SSN/EIN/email/phone
   patterns, sensitive JSON field names. Any finding refuses the file wholesale —
   exclusion over redaction, no override flag (ADR 0007). `kb_dump` is refused
   (raw SSN/PAN in 5 of 6 records, EIN in the 6th).
2. **Ingest** (`chunker.py`) — gate-passed markdown only; one chunk per `##`
   section, stable ids (`doc#section-slug`).
3. **Embed** (`embedder.py`, `cache.py`) — the embedding backend behind a minimal
   `fit/embed/signature` interface, selected by `RAG_EMBEDDER` (see **Backends**).
   Default is pure-Python TF-IDF; Bedrock is the drop-in scaling backend, and
   callers are unchanged either way. Vectors cached on disk keyed by content hash
   (signature + text); the cache stores term-weight/float vectors only, never chunk
   bodies or PII.
4. **Retrieve + score** (`index.py`, `metrics.py`) — in-memory exact cosine index,
   rebuilt each run; hit@1/3/5 and MRR per query and aggregate over
   `gold_queries.json` (10 answerable officer questions + the #6012 trap + an
   off-corpus control).
5. **Report** (`report.py`) — metrics, hygiene verdicts (masked samples only), the
   calibrated unanswerable-confidence threshold and its method, and a **Data gaps**
   section: why "why was #6012 denied?" is unanswerable by design
   (`decisions(app_id, outcome)` records no reason — see ADR 0008), and the
   false-confident-retrieval risk for a naive helper.

## Backends

The embedding backend is chosen by `RAG_EMBEDDER` (default `tfidf`). All other
stages — gate, chunker, cache, index, metrics, report — are identical across
backends; only the vector shape differs (sparse dict vs dense list), and
`cosine()` handles both.

| `RAG_EMBEDDER` | Backend | Deps | Auth |
|----------------|---------|------|------|
| `tfidf` (default) | Pure-Python TF-IDF | none (stdlib) | none |
| `bedrock` | Amazon Bedrock dense embeddings | `boto3` | AWS credentials |

TF-IDF is the CI/offline default — keyless, deterministic, no network. Bedrock is
the **scaling path** (Phase 1 of `docs/PHASE1-BEDROCK-PGVECTOR.md`):

```bash
pip install -r rag_eval/requirements-bedrock.txt
export RAG_EMBEDDER=bedrock
export AWS_REGION=us-east-1                        # or your enabled region
# AWS creds via env/profile/role (never an API key literal); optionally:
# export RAG_BEDROCK_MODEL=amazon.titan-embed-text-v2:0   # the default
python3 -m rag_eval.run
```

Switching backends changes the embedder `signature`, which cleanly invalidates
the on-disk cache (nothing from one backend is ever compared against another).
The confidence threshold recalibrates automatically per run and is recorded, with
the backend signature, in the report.

The retrieval index stays **in-memory** for both backends (ADR 0007 rule 6). A
persistent pgvector store is **Phase 2**, gated on corpus growth (~hundreds+ of
chunks), not on the embedding swap — see `docs/PHASE1-BEDROCK-PGVECTOR.md`.

## Tests

```bash
python3 -m pytest rag_eval/tests -q        # unit + integration (real corpus)
```

Tests never call Bedrock: `BedrockEmbedder` takes an injected client, so the
suite stays keyless and offline.

## Known limits

- TF-IDF (the default) is lexical: no stemming/synonyms. Adequate at 9 chunks
  (hit@3 = 1.0 on the gold set). When the corpus grows, switch to the dense
  Bedrock backend (`RAG_EMBEDDER=bedrock`) — no caller changes, just recalibrate.
- The confidence threshold is calibrated to this gold set; re-calibrate on any
  corpus or backend change (method recorded in the report).
- The gate is a floor, not a ceiling (regex+Luhn misses novel PII shapes); corpus
  additions still require human curation (ADR 0007).
