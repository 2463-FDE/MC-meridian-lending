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
3. **Embed** (`embedder.py`, `cache.py`) — pure-Python TF-IDF behind a minimal
   `fit/embed/signature` interface (swap in a dense backend later without touching
   callers). Vectors cached on disk keyed by content hash; the cache stores
   term-weight vectors only, never chunk bodies or PII.
4. **Retrieve + score** (`index.py`, `metrics.py`) — in-memory exact cosine index,
   rebuilt each run; hit@1/3/5 and MRR per query and aggregate over
   `gold_queries.json` (10 answerable officer questions + the #6012 trap + an
   off-corpus control).
5. **Report** (`report.py`) — metrics, hygiene verdicts (masked samples only), the
   calibrated unanswerable-confidence threshold and its method, and a **Data gaps**
   section: why "why was #6012 denied?" is unanswerable by design
   (`decisions(app_id, outcome)` records no reason — see ADR 0008), and the
   false-confident-retrieval risk for a naive helper.

## Tests

```bash
python3 -m pytest rag_eval/tests -q        # unit + integration (real corpus)
```

## Known limits

- TF-IDF is lexical: no stemming/synonyms. Adequate at 9 chunks (hit@3 = 1.0 on
  the gold set); the `Embedder` interface exists so a dense backend can replace it
  when the corpus grows.
- The confidence threshold is calibrated to this gold set; re-calibrate on any
  corpus or backend change (method recorded in the report).
- The gate is a floor, not a ceiling (regex+Luhn misses novel PII shapes); corpus
  additions still require human curation (ADR 0007).
