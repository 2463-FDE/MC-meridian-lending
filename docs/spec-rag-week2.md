# Spec: RAG & Knowledge Retrieval (Week 2)

**Owner:** Dana (VP Lending Ops)
**Date:** 2026-07-07
**Status:** Draft — pending gate approval

---

## Executive Summary

Loan officers repeatedly ask compliance the same underwriting-policy questions. Dana wants
a helper that answers from the lending-policy docs and past decisions. Before any helper is
built, this week delivers the **retrieval eval harness** that proves what the corpus can and
cannot answer — and surfaces two blocking problems in the data Dana handed over:

1. **The knowledge-base dump is radioactive.** `kb_dump/applications.jsonl` contains raw
   SSN, PAN, DOB, name, and address in every record — unredacted, in the same corpus Dana
   expects us to embed into a vector store.
2. **"Why was #6012 denied?" is unanswerable by design.** The `decisions` table records
   `(app_id, outcome)` only — no reason, no drivers, no timestamp. The empty retrieval is
   not a retrieval bug; the answer was never recorded anywhere. The policy docs themselves
   flag this ("reasons are produced ad hoc at letter-generation time").

**This week's scope:** eval harness + corpus-hygiene gate + two ADRs. The officer-facing
helper (chat endpoint) is **not** this week.

---

## Problem Statement

**Surface request:** "Build a helper that answers from our lending-policy docs and our past
decisions. One officer asked 'why was app #6012 denied?' and it came back empty."

**Real problem:** Two data problems must be fixed before a RAG helper is safe or useful:
embedding the handed-over corpus would push raw PAN/SSN into a vector store (a new PCI/PII
liability, compounding debt items D5/D13), and the decision-reason data the officers most
want does not exist. A helper built today would leak PII and still fail the #6012 question.

**Constraint (quota/cost):** Dana is "on basically a Pro plan." Embed only a sampled policy
subset, cache embeddings on disk, never re-embed unchanged content per run, and do all
redaction/hygiene checks offline (regex/validator) — zero LLM calls in the harness.

---

## Deliverables (In Scope)

### D1. Retrieval Eval Harness

A standalone, runnable harness (no service wiring) that ingests the policy corpus, runs a
gold query set through retrieval, and emits a markdown report.

**Acceptance:**
1. Harness runs offline with one command (e.g. `python -m rag_eval.run`) and makes **zero
   LLM API calls**.
2. Ingest pipeline: loads `policies/*.md`, chunks by markdown section, embeds chunks, and
   builds an in-memory index.
3. Embeddings are **cached on disk keyed by content hash**; a second run with unchanged
   corpus re-embeds nothing (verifiable in the run log/report).
4. Gold query set (checked-in file) of ≥ 10 labeled queries: each maps to expected source
   chunk(s), or is labeled `unanswerable` (e.g. "why was application #6012 denied?").
5. Report computes retrieval metrics per query and aggregate: hit@k (k=1,3,5), MRR, and
   per-query retrieved-vs-expected detail.
6. The report has a **"Data gaps"** section that explicitly states: the #6012 question is
   unanswerable because `decisions(app_id, outcome)` records no reason/drivers/timestamp —
   citing table schema and the guidelines' own operational note. It must also cite the only
   trace that exists: `logs/payment-service.log:14`, an unstructured log line recording
   `adverse_action_reason="purchasing history"` — ephemeral, non-queryable, not valid Reg B
   principal-reason language, and contradicting policy (model_score=612 falls in the
   600–659 *refer* band, yet the outcome was deny).

### D2. Corpus Hygiene Gate (Redaction Check on Ingest)

An offline validator that scans every candidate corpus file **before** embedding and
refuses contaminated sources.

**Acceptance:**
1. Detects, via regex + Luhn validation (no LLM): PAN (13–19 digits, Luhn-valid), SSN,
   DOB-shaped dates in identity context, email, phone, and sensitive JSON field names
   (`ssn`, `pan`, `dob`, `ein`).
2. Running the gate over `kb_dump/applications.jsonl` **fails/refuses** that file and the
   report lists finding counts per PII type (expected: SSN + PAN in 5 of 6 records, EIN in
   the 6th).
3. Running the gate over `policies/*.md` passes (clean policy docs enter the corpus).
4. The harness **never embeds a file that fails the gate** — enforced in code, not by
   convention.
5. Hygiene findings appear in the eval report alongside retrieval metrics.
6. Validator is unit-tested in isolation (PAN/Luhn true+false positives, SSN, field-name
   detection, clean-text pass-through).

### D3. ADR: Corpus Hygiene Policy

**Acceptance:**
1. `adr/0007-rag-corpus-hygiene.md` (next free number) — Accepted status.
2. Locks: what may enter a retrieval corpus (curated policy docs; future structured
   decision records without direct identifiers) and what may never (raw application
   records, PAN/CVV/SSN/DOB in any form); ingest gate is mandatory; embeddings cached;
   redaction checks offline.

### D4. ADR: Retrievable Decision-Record Requirement

**Acceptance:**
1. `adr/0008-retrievable-decision-records.md` — Accepted status.
2. Requires every decision to record: principal reason(s) (Reg B adverse-action language),
   model/policy drivers (score, DTI, cutoff applied), decision timestamp, and decider
   (system/user) — so "why was #X denied?" is answerable from stored data.
3. Names the schema gap (`decisions(app_id, outcome)` in `db/init/001_schema.sql`) and the
   migration path (additive columns; backfill impossible for past denials — say so).
4. Ties to compliance: Reg B requires specific principal reasons on adverse action; today a
   regulator asking for the reason behind denial #6012 cannot be answered from this data.

---

## Out of Scope (Not This Week)

- The officer-facing helper itself (chat/QA endpoint, answer generation, any Claude call).
- Applying the decisions-schema migration (ADR 0008 specifies it; implementation is Week 3+).
- Embedding past decisions (blocked until ADR 0008 fields exist and records pass the gate).
- Vector database infrastructure (in-memory index is sufficient for eval).
- Redacting-then-embedding kb_dump (rejected — see ADR 0007; exclusion, not redaction).

---

## Acceptance Criteria (Roll-up)

### Functional
1. Eval harness runs end-to-end offline with cached embeddings (D1.1–D1.3).
2. Gold query set with unanswerable-query handling (D1.4–D1.5).
3. Report surfaces the decision-record gap (D1.6) and PII findings (D2.5).
4. Hygiene gate refuses `kb_dump/applications.jsonl`, passes `policies/*.md` (D2.2–D2.4).

### Security/Compliance
5. No PAN/SSN/DOB is ever embedded, cached, logged, or written to the report in plaintext
   (findings are reported as counts + masked samples, never raw values).
6. Zero LLM API calls anywhere in the harness (cost + data-exfiltration guard).

### Process
7. Work on `feature/rag-eval-week2` off `main`; small commits traceable to spec sections.
8. ADR 0007 and ADR 0008 committed before harness implementation starts.
9. Unit tests pass for hygiene validator, chunker, cache, and metrics.
10. Integration test: full harness run over the real `policies/` + `kb_dump/` produces a
    report containing both required findings.

---

## Notes for Implementation

- Embedding backend must be local/zero-API-cost (see Stage 1 decision ledger).
- Reuse Week 1 `PiiRedactor` regex patterns for detection; PAN detection adds Luhn.
- Sampled subset: the two policy docs are already small (68 lines total); "sampling" =
  section-level chunks, embedded once, cached.
- Gold queries should mirror real officer questions: approval cutoffs, DTI definition,
  refer band, adverse-action timing, late-fee amount, NSF fee, note-rate range, payment
  waterfall, plus the #6012 trap and one off-corpus question (expected: no answer).

## Success Metrics

- Dana gets a concrete, runnable answer to "why did #6012 come back empty" — with evidence.
- Compliance gets an ingest gate that provably blocks the PII dump before any vector store exists.
- Week 3+ helper work starts on a corpus that is measured (metrics) and clean (gate).
