# ADR 0008: Retrievable Decision Records — Every Decision Must Record Its Reasons

- **Status:** Accepted
- **Date:** 2026-07-07
- **Author:** Claude Code
- **Related:** ADR 0007 (corpus hygiene), docs/spec-rag-week2.md (D4), Reg B adverse action

---

## Context

A loan officer asked the prototype helper "why was application #6012 denied?" and got zero
retrieved documents. Investigation shows this is not a retrieval failure — **the answer was
never recorded**:

- `decisions` table (`db/init/001_schema.sql:59`) stores `(app_id, outcome)` only — no
  reason, no model drivers, no timestamp, no decider. The seed data says it outright:
  *"Denials 6012/6013 have no recorded reason anywhere"* (`db/init/002_seed.sql:38`).
- The only trace in the whole estate is an **unstructured log line**
  (`logs/payment-service.log:14`):
  `GET /decision app_id=6012 model_score=612 decision=deny adverse_action_reason="purchasing history"`.
  Logs are ephemeral, non-queryable, and per ADR 0006 subject to redaction — not a system
  of record.
- That lone trace is itself non-compliant and contradictory: "purchasing history" is not a
  specific principal reason in Reg B terms, and a model score of 612 falls in the policy's
  **refer band (600–659)** per `policies/underwriting_guidelines.md` — yet the recorded
  outcome is deny, with no explanation of the override.
- The underwriting guidelines already flag the practice: *"the tool currently records the
  outcome of a decision but the reasons are produced ad hoc at letter-generation time."*
- An officer's note confirms the operational pain: *"We approve/deny in the tool, but I can
  never find why later."*

**Regulatory stake:** Reg B requires an adverse-action notice stating the specific
principal reason(s) for denial, and records retention (~25 months). If a regulator asked
today for the reason behind denial #6012, Meridian could not produce it from stored data.

## Decision

Every credit decision MUST be persisted as a **retrievable decision record** containing:

| Field | Content |
|-------|---------|
| `app_id` | Application the decision applies to |
| `outcome` | approve / refer / deny / counteroffer |
| `principal_reasons` | One or more specific Reg B principal reasons (structured list, adverse-action vocabulary) |
| `drivers` | Model/policy drivers: model score, DTI, the cutoff or band applied, fraud flag |
| `policy_band` | Band the score/DTI actually landed in (approve/refer/deny) — makes overrides visible |
| `decided_at` | Timestamp |
| `decided_by` | System (model + version) or user id for manual/override decisions |

Requirements:

1. **Schema change (additive):** extend `decisions` with the columns above (nullable for
   legacy rows). Migration lives in `db/migrations/` and `db/init/001_schema.sql`.
   Implementation is scheduled Week 3+; this ADR locks the contract now so the Week 2 eval
   harness can state the gap precisely and the RAG corpus design (ADR 0007) can plan for an
   identifier-free projection of these records.
2. **Write-path rule:** decision-service must not persist an outcome without
   `principal_reasons` and `drivers`. An outcome that contradicts `policy_band` (e.g. deny
   in the refer band) requires `decided_by` to be a user — silent system overrides are
   forbidden.
3. **Retrievability:** the identifier-free projection (no name/SSN/PAN/DOB/address —
   ADR 0007 rule 1) is what may be indexed for the officer helper, making "why was #X
   denied?" answerable from stored data.
4. **Backfill is impossible and must be said plainly:** reasons for past denials (6012,
   6013) were never captured; no migration can recover them. Historical rows remain
   reason-less and the eval report must not pretend otherwise.

## Consequences

### Positive
- "Why was #X denied?" becomes answerable — for officers via retrieval and for regulators
  via SQL — from the decision date forward.
- Reg B adverse-action letters can be generated from recorded reasons instead of ad hoc
  prose at letter time.
- Policy overrides become visible (`policy_band` vs `outcome` + `decided_by`), surfacing
  cases like 6012's refer-band denial.

### Negative
- decision-service write path gains a hard validation requirement; deciding gets slightly
  more expensive operationally (reasons must be chosen, not implied).
- Legacy rows stay unanswerable forever — the helper must distinguish "no record (legacy)"
  from "not found," or officers will keep reading data gaps as search bugs.
- Cross-service coordination: decision-service owns the write, but origination boards and
  servicing reads — schema change rides on the shared-database seam (ADR 0002/0004 debt).
