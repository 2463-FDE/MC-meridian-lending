# Stage 1 Plan: RAG & Knowledge Retrieval (Week 2)

**Date:** 2026-07-07
**Spec:** docs/spec-rag-week2.md
**Branch:** feature/rag-eval-week2 (off main)
**Status:** Ready for gate approval

---

## Gap Analysis

### D1: Retrieval Eval Harness

**Current state:**
- `policies/underwriting_guidelines.md` (42 lines) and `policies/fee_schedule.md` (26 lines)
  exist and are clean — sectioned markdown, no PII.
- `kb_dump/applications.jsonl`: 6 records; 5 contain raw `ssn`, `pan`, `dob`, `name`,
  `address`; the 6th (entity, app 6014) contains raw `ein`.
- `decisions` table (`db/init/001_schema.sql:59`): `(app_id, outcome)` only. Seed comment is
  explicit: "Denials 6012/6013 have no recorded reason anywhere."
- The *only* trace of a denial reason in the estate: `logs/payment-service.log:14–15` —
  unstructured log lines with `adverse_action_reason="purchasing history"` (not valid Reg B
  language; and 6012's `model_score=612` is in the 600–659 refer band per
  `policies/underwriting_guidelines.md`, contradicting the recorded deny outcome).
- No retrieval, embedding, chunking, eval, or index code exists anywhere in the repo.

**Gap:** entire harness is new: chunker, embedder + disk cache, in-memory index, gold query
set, metrics (hit@k, MRR), markdown report writer, data-gaps section.

### D2: Corpus Hygiene Gate

**Current state:**
- Week 1 built `PiiRedactor` (regex masking for PAN/CVV/SSN/email/phone) in each service's
  `app/redactor.py` — but that branch (`feature/pii-redaction`) is **unmerged**, so nothing
  is importable from `main`. An uncommitted Luhn upgrade to it sits in a stash.
- Redactor *masks* text; hygiene gate must *detect and refuse* files — different behavior,
  same patterns.

**Gap:** new detection-mode validator (regex + Luhn + JSON field-name checks), file-level
verdicts (pass/refuse), wired as a hard precondition of ingest.

### D3 / D4: ADRs

**Current state:** ADRs 0001–0006 exist; next free numbers 0007/0008. No ADR covers corpus
content policy or decision-record content. Guidelines already flag the reason gap
(`policies/underwriting_guidelines.md:30–31` operational note).

**Gap:** write ADR 0007 (corpus hygiene) and ADR 0008 (retrievable decision records).

---

## Decision Ledger

### DL-1: Embedding backend

| Option | Tradeoffs |
|--------|-----------|
| API embeddings (Voyage/OpenAI) | Best quality; **spends quota, sends corpus off-box, violates cost constraint** |
| Local sentence-transformers (MiniLM) | Real dense embeddings, zero API cost; ~1 GB torch dependency chain |
| Local lexical: TF-IDF (scikit-learn) | Zero API cost, small dependency, deterministic; weaker semantic matching |
| Local lexical: TF-IDF (pure Python) | Zero API cost, **zero third-party deps**, deterministic; same retrieval quality as sklearn TF-IDF at this corpus size |

**Chosen:** **pure-Python TF-IDF + cosine similarity behind a small `Embedder` interface**,
with the interface designed so a dense backend can be dropped in later.
**Why:** the corpus is 2 documents / ~15 chunks; at this scale lexical retrieval is
competitive and the eval harness's job is to measure retrieval and surface data gaps — not
to ship the production retriever. Pure Python honors the quota constraint absolutely (zero
API, zero downloads), is deterministic (stable eval numbers), runs anywhere (the dev
machine has no sklearn/numpy), and the interface keeps the Week 3 upgrade path open. The
cache requirement (content-hash keyed) applies identically. Harness deps: stdlib + pytest.

### DL-2: Chunking strategy

Options: fixed-size token windows vs markdown-section chunks.
**Chosen:** one chunk per markdown `##` section (plus doc title context), with
`{doc, section, chunk_id}` metadata.
**Why:** both policy docs are already semantically sectioned and small; fixed windows would
split tables (fee schedule) mid-row and add tuning surface with no benefit at this size.

### DL-3: Where the code lives

Options: inside origination-service vs new top-level `rag_eval/` package.
**Chosen:** top-level `rag_eval/` with its own `requirements.txt` and tests.
**Why:** it is an offline analysis tool, not a runtime service — no FastAPI, no DB, no
docker-compose entry. Putting it in a service would couple eval runs to service deps and
imply a runtime role it doesn't have. Week 3's helper service can import or vendor it.

### DL-4: kb_dump handling — exclude vs redact-then-embed

**Chosen:** **exclude entirely** (gate refuses the file).
**Why:** redaction masks pattern-matched fields but names/addresses resist regex; residual
identity risk in a vector store is unacceptable (compounds debt D5/D13). And the records
carry no answer content anyway — outcome without reason. Redact-then-embed buys risk for
zero retrieval value. ADR 0007 locks this.

### DL-5: PII detection implementation

**Chosen:** self-contained `rag_eval/hygiene.py` re-using Week 1 regex patterns + Luhn
check, plus JSONL field-name detection (`ssn`/`pan`/`dob`/`ein` keys).
**Why:** `feature/pii-redaction` is unmerged — cannot import from main. Field-name checks
catch structured PII even where value regexes miss (e.g. `"dob": "1992-04-21"` is just a
date by value). Duplication is noted in the module docstring with a pointer to consolidate
after the pii-redaction PR merges.

### DL-6: Unanswerable-query semantics in eval

**Chosen:** gold set supports `expected: unanswerable`; a query scores correct when
retrieval confidence falls below threshold / no expected chunk exists, and the report
routes it to the "Data gaps" section with a stated root cause.
**Why:** the #6012 case is the week's headline finding. Scoring it as a plain retrieval
miss would mislabel a data-capture failure as a search failure — the exact confusion Dana
already has ("which was weird").

---

## Implementation Plan (order of work)

| # | Step | Files | Traces to |
|---|------|-------|-----------|
| 1 | Commit spec + this plan | `docs/spec-rag-week2.md`, `docs/STAGE1-PLAN-RAG-WEEK2.md` | Process 7 |
| 2 | ADR 0007 corpus hygiene | `adr/0007-rag-corpus-hygiene.md` | D3, Process 8 |
| 3 | ADR 0008 retrievable decision records | `adr/0008-retrievable-decision-records.md` | D4, Process 8 |
| 4 | Hygiene validator + unit tests | `rag_eval/hygiene.py`, `rag_eval/tests/test_hygiene.py` | D2.1–D2.3, D2.6 |
| 5 | Chunker + unit tests | `rag_eval/chunker.py`, tests | D1.2 |
| 6 | Embedder interface + TF-IDF backend + content-hash cache + tests | `rag_eval/embedder.py`, `rag_eval/cache.py`, tests | D1.2–D1.3, DL-1 |
| 7 | Index + retrieval + metrics (hit@k, MRR) + tests | `rag_eval/index.py`, `rag_eval/metrics.py`, tests | D1.5 |
| 8 | Gold query set (≥10, incl. #6012 + off-corpus) | `rag_eval/gold_queries.json` | D1.4, DL-6 |
| 9 | Runner + report writer (gate enforced before embed; data-gaps + hygiene sections) | `rag_eval/run.py`, `rag_eval/report.py` | D1.1, D1.6, D2.4–D2.5 |
| 10 | Integration test: real corpus end-to-end, assert both findings in report | `rag_eval/tests/test_integration.py` | Process 10 |
| 11 | README for the harness + tracker update | `rag_eval/README.md`, `docs/feature-status-tracker.md` | Process |

Each step = one commit. `.gitignore` gains `rag_eval/.cache/`.

**Test strategy:** unit per module (steps 4–7); integration = one full run over real
`policies/` + `kb_dump/` (step 10). Smoke (Stage 8) = run the actual CLI command twice,
assert second run reports 0 re-embeds, and grep the generated report for the two required
findings and for absence of any raw SSN/PAN value.

---

## Traceability

| Spec item | Plan step(s) |
|-----------|--------------|
| D1.1 zero-LLM one-command run | 9 |
| D1.2 ingest pipeline | 5, 6, 7 |
| D1.3 embedding cache | 6 |
| D1.4 gold query set | 8 |
| D1.5 metrics | 7 |
| D1.6 data-gaps section | 9 |
| D2.1 detection patterns | 4 |
| D2.2 kb_dump refused | 4, 9 |
| D2.3 policies pass | 4, 9 |
| D2.4 gate enforced in code | 9 |
| D2.5 findings in report | 9 |
| D2.6 validator unit tests | 4 |
| D3 ADR 0007 | 2 |
| D4 ADR 0008 | 3 |
| Sec/Comp 5 (no plaintext PII in outputs) | 4, 9, 10 |
| Sec/Comp 6 (zero LLM calls) | 6 (local backend), 9 |
| Process 7–10 | 1–3, 4–10 |

No plan item lacks a spec anchor — no scope creep detected. Form polish and the chat helper
are explicitly out of scope.

---

## Open Questions (non-blocking unless noted)

1. **Gold-set answer keys:** I will author the ≥10 queries + expected chunks from the
   policy docs. Sign-off happens at the Stage 2 verification gate.
2. **Left over from Week 1 (not this feature):** uncommitted Luhn redactor work is stashed
   on `feature/pii-redaction` (`git stash list` → "wip: luhn PAN redaction"); payment-service
   integration tests fail locally on that branch even without the stash (5 failures,
   pre-existing) despite the tracker's "Integration ✓". Needs a revisit before that PR merges.
