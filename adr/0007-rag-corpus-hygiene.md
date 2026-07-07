# ADR 0007: RAG Corpus Hygiene — What May Enter a Retrieval Corpus

- **Status:** Accepted
- **Date:** 2026-07-07
- **Author:** Claude Code
- **Related:** ADR 0006 (logging redaction), docs/spec-rag-week2.md (D2, D3), debt items D5/D13

---

## Context

Dana (VP Lending Ops) wants a helper that answers underwriting-policy questions from
"the policy folder plus a big knowledge base of past applications." The handed-over
corpus is:

- `policies/underwriting_guidelines.md`, `policies/fee_schedule.md` — clean, sectioned
  policy docs. No PII.
- `kb_dump/applications.jsonl` — 6 past-application records. **Five contain raw `ssn`,
  `pan`, `dob`, `name`, and `address`; the sixth (an entity) contains a raw `ein`.**

Embedding this dump would copy full SSNs and card numbers into a vector store and its
on-disk caches — a *new* PCI/PII surface on top of existing debt (D5: plaintext PII in
logs; D13: PAN/CVV columns in the payments table). Vector stores and embedding caches are
backup targets exactly like log files, and embedded text is recoverable from most stores
(they persist the source chunk alongside the vector).

Cost constraint from the client: "basically a Pro plan" — embed a sampled policy subset,
cache embeddings, never re-embed per run, and run hygiene checks offline (regex/validator),
not via LLM calls.

## Decision

1. **Allowed into a retrieval corpus:**
   - Curated policy documents (the `policies/` docs and successors) after passing the
     hygiene gate.
   - Future **structured decision records** — only once ADR 0008's fields exist, and only
     in an identifier-free projection (app_id, outcome, principal reasons, drivers,
     timestamp — never name/SSN/PAN/DOB/address).

2. **Never allowed, in any form:** raw application records; PAN, CVV, SSN, DOB, bank
   account numbers; free-text fields that embed identity (name + address). This applies to
   the vector store, embedding caches, eval reports, and harness logs alike.

3. **Exclusion, not redaction, for contaminated dumps.** `kb_dump/applications.jsonl` is
   refused wholesale rather than redacted-then-embedded. Regex redaction cannot reliably
   mask names and addresses, so residual identity risk remains; and these records carry no
   answerable content anyway (outcome without reason — see ADR 0008). Redact-then-embed
   buys risk for zero retrieval value.

4. **A mandatory ingest gate enforces this in code.** Every candidate file is scanned by an
   offline validator (regex + Luhn for PAN; SSN/email/phone patterns; sensitive JSON field
   names `ssn`/`pan`/`dob`/`ein`) **before** any chunking or embedding. A file that fails
   is not embedded — the pipeline refuses it and reports finding counts with masked
   samples. There is no override flag.

5. **Embedding cost discipline:** embeddings are computed once per content-hash and cached
   on disk; unchanged content is never re-embedded. Hygiene checks are pure regex/validator
   logic — zero LLM calls.

## Consequences

### Positive
- No PII ever reaches a vector store, cache, or report; the PCI/PII debt surface does not
  grow with the RAG work.
- The gate is testable in isolation and its findings are auditable (counts per type, file
  verdicts in every eval report).
- Cost stays near zero: local validation, cached local embeddings, no API spend.

### Negative
- Past applications contribute nothing to the helper until structured, identifier-free
  decision records exist (ADR 0008) — the "past decisions" half of Dana's ask is blocked
  on a data-model fix, not on retrieval engineering. This must be communicated.
- Regex+Luhn detection has known limits (novel PII shapes, free-text identity). The gate is
  a floor, not a ceiling; corpus additions still require human curation.
- No override flag means a false-positive refusal blocks ingest until the validator is
  fixed — accepted: fail-closed is the correct posture for cardholder data.
