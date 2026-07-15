# ADR 0009: Decisioning Assistant — Deterministic Scoring Core, LLM Narration, Feature→Reason Mapping

- **Status:** Accepted by product + engineering; compliance/legal and data-owner reviews
  **pending** (see *Sign-off status*). Design is locked for build; the pending reviews gate
  production rollout, not implementation on the feature branch.
- **Date:** 2026-07-15
- **Author:** Claude Code
- **Related:** ADR 0005 (LLM client), ADR 0006 (logging redaction), ADR 0007 (identifier-free
  projection), ADR 0008 (decision-record field contract), docs/spec-decision-assistant-week3.md,
  CFPB Circular 2023-03

---

## Context

Dana (VP Lending Ops) asked for an assistant that "decisions an application and tells the
officer the result," wrapping a newly licensed AI credit-scoring model. No such model
artifact exists — it is hypothetical; this feature defines a deterministic stand-in with
the vendor output shape.

The current decision path is non-compliant with Reg B adverse-action requirements:

- Every deny/refer carries the same generic reason string ("purchasing history",
  `services/decision-service/app/decision.py`), unrelated to what the model weighed.
- Only `(app_id, outcome)` is persisted — no inputs, drivers, reasons, or timestamp
  (`decisions` table). A disputed denial is unprovable from stored data.
- Denial #6012 scored 612 (refer band per `policies/underwriting_guidelines.md`) but was
  recorded as deny — an invisible, unattributed override.

ADR 0008 locked the decision-record **field contract** in Week 2 and deferred
implementation to Week 3+. This ADR is the Week 3 implementation decision record.

### Sign-off status (per ADR 0008's four-owner gate)

| Owner (role) | Status |
|--------------|--------|
| Product | **Approved** — Dana's 2026-07-15 directive (this feature's brief) is recorded as the product approval of the record's business fields and the officer use case |
| Engineering | **Approved** — this ADR (write-path validation + policy-band override enforcement design, section 4) |
| Compliance / Legal | **Pending** — Reg B citations remain non-authoritative (carried from ADR 0008); reason *texts* in section 3 require their review before production use |
| Data owner | **Pending** — `decision_events` is additive-only and touches no existing rows, minimizing schema risk, but the shared-DB seam approval (ADR 0002/0004) is still theirs to give |

Implementation proceeds on the feature branch with the two pending reviews explicitly
open; both must close before production rollout.

## Decision

### 1. The LLM never scores credit; a deterministic model decides

The credit decision is computed by a deterministic scoring module, never by an LLM.
LLM-based scoring was considered and **rejected**:

- Nondeterministic — a regulated decision must be reproducible on demand.
- Its "explanations" are generated prose, not attributions of what the computation
  actually weighed — a fluent wrong reason is worse than a generic one.
- Fair-lending risk: an LLM encodes untestable proxies for protected classes (ECOA
  disparate impact); CFPB Circular 2023-03 removes any "the AI decided" defense.
- Ungovernable under model-risk expectations (SR 11-7): no input-range validation,
  no monotonicity guarantees, silent vendor weight changes.
- Prompt-injectable via applicant-supplied free text.
- Adds per-decision cost and seconds of latency to a chain that already times out
  past ~20 concurrent applications.

The LLM's role is **language only**: orchestrate tools and narrate recorded facts to the
officer. It reads decisions; it never creates them.

### 2. Vendor model stand-in ("meridian-risk-stub v1")

A deterministic module in decision-service emits the licensed-vendor output shape:

- `score` (int), `model_id`/`model_version`, and **ranked signed feature attributions**.
- Same input → same output; unit-tested for determinism.
- Behind a small interface so a real vendor model can replace it without touching the
  write path.

Features are computed from data the platform actually holds (`applications`:
`amount`, `term_months`, `income`, `employment_years`; bureau pull: `bureau_score`):

| Feature | Derived from | Direction |
|---------|--------------|-----------|
| `delinquency_history` | bureau score band | low score → negative |
| `payment_burden` | est. monthly payment (`amount/term_months`) vs monthly income | high ratio → negative |
| `income_sufficiency` | income vs amount requested | low ratio → negative |
| `employment_tenure` | `employment_years` | short tenure → negative |

Scoring stays calibrated to the existing policy bands (approve ≥ 660, refer 600–659,
deny < 600) so seed data and downstream behavior remain coherent.

### 3. Feature → adverse-action reason mapping (locked)

Every feature the model can emit maps to a specific Reg B principal-reason code. When the
outcome is deny or refer, the persisted `principal_reasons` are derived from the
applicant's **actual top negative attributions** — different drivers yield different
reasons. The generic "purchasing history" string must be deleted from the codebase as
part of this feature's implementation.

| Feature (top negative) | Reason code | Reason text |
|------------------------|-------------|-------------|
| `delinquency_history` | `R01` | Delinquent past or present credit obligations with others |
| `payment_burden` | `R02` | Excessive obligations in relation to income |
| `income_sufficiency` | `R03` | Income insufficient for amount of credit requested |
| `employment_tenure` | `R04` | Length of employment |

Rules:

- **Fail closed on unmapped features:** a model emitting a feature with no reason mapping
  is a contract violation — the decision is refused, not issued with a fallback reason.
  This is the integration gate for any future real vendor model: no mapping, no go-live.
- Reason *texts* use adverse-action vocabulary and are subject to the open
  compliance/legal review; the *mechanism* (top-attribution-derived, per-applicant) is
  what this ADR locks.

### 4. Decision memory: append-only `decision_events`

Implements ADR 0008's field contract as a **new append-only table** rather than extending
`decisions` in place (an explicit deviation from ADR 0008's "extend `decisions`" wording —
an upserted row cannot be append-only, and an audit trail must survive re-decisions;
the field contract itself is honored verbatim):

- `decision_events`: `app_id`, `outcome`, `principal_reasons`, `drivers` (score, top
  attributions, band cutoff applied), `policy_band`, `inputs` (identifier-free per
  ADR 0007 rule 1 — no SSN/name/PAN/DOB/address), `decided_by` (model id+version, or
  user id for overrides), `decided_at`. Additive migration; `db/init/001_schema.sql`
  and `db/migrations/` both updated.
- `decisions` remains as the mutable current-state pointer; `decision_events` is the
  system of record. No UPDATE/DELETE code path is permitted for it.
- **Write-path rule (ADR 0008 req. 2, to be enforced by this feature's write path):**
  no outcome persists without `principal_reasons` and `drivers`; an outcome contradicting
  `policy_band` requires a human `decided_by` — the system cannot silently override
  (prevents recurrence of the #6012 class).
- Legacy rows (#6012, #6013) are unrecoverable; the assistant must answer
  "no record (legacy)" — distinct from "not found" — never invent reasons.

### 5. Agent architecture: regulated write inside the score tool

Single agent, hosted in origination-service (where the ADR 0005 LLM client lives;
decision-service stays LLM-free), running on `ClaudeClient`.

**Amendment (2026-07-15, at implementation):** tool use is implemented as a
**prompt-level JSON action protocol**, not Anthropic API-native tool blocks. Each turn
the model returns one schema-validated JSON object — `{action: "tool", tool, input}` or
`{action: "final", outcome, reason_codes, summary}` — and a deterministic loop in code
executes the tool and feeds its result back as a JSON-object history turn. Why the
deviation from the original "extend the client with API tool blocks" intent:

- The ADR 0005 redaction pipeline requires every history turn to be a JSON **object**
  and fails closed on free strings. Tool results are JSON objects of enum codes and
  numbers, so they ride the existing fail-closed path verbatim; API-native tool blocks
  would need a new content-block shape threaded through builder, adapter, validator,
  and FakeAdapter — new attack surface on a hardened seam, for no capability this
  feature needs.
- Every agent turn goes through the untouched `complete()` path: token budget, retry,
  schema validation, output PII guards all apply per step.
- Tool-result vocabulary (outcome/band/status/reason codes) is admitted via the
  redactor's designed `_SAFE_CATEGORICAL` extension point; adverse-action reason texts
  reach the model only via the authored system prompt, never as caller data.

API-native tool blocks remain future work if a feature needs parallel or streaming
tool calls.

The agent's tools:

- **Score tool** → decision-service decisioning endpoint. The `decision_events` write
  happens **inside this call, atomically with the decision**. The LLM cannot decision an
  application without the record being written — a compliance write an agent could skip
  is not a control. The tool takes only an application id; applicant data is looked up
  by code, never supplied by the model.
- **Decision-memory tool** → retrieves the persisted record by `app_id`
  (identifier-free projection).
- The agent's officer-facing answer is **validated against the persisted event** before
  returning: on mismatch, the recorded facts are returned, never the narration.
- All prompts/tool results pass the ADR 0005/0006 redaction pipeline; tool results are
  identifier-free by construction.

### 6. Sync → async (documented, deferred)

The decisioning chain (bureau pull → model → persist) runs synchronously on the request
thread; origination blocks on it over HTTP with no timeout/retry contract, and load
testing shows timeouts past ~20 concurrent applications. Dana prioritized shipping the
assistant; async is **explicitly deferred**, not forgotten:

- Target shape: origination submits a decision *request* (queue or outbox event) and
  returns immediately; decision-service consumes, decisions, appends the event record,
  and notifies (callback/poll). The `decision_events` table introduced here is the
  natural outbox — the async migration changes *when* the record is written, not *what*.
- Until then, concurrency above ~20 remains a known failure mode (debt-log candidate;
  same family as the ADR 0002/0004 synchronous-coupling debt).

## Consequences

### Positive
- "Why was #X denied?" answerable from stored data — for officers via the agent's memory
  tool, for regulators via SQL — from this feature's deploy date forward.
- Adverse-action reasons are specific, accurate, and traceable to model attributions;
  letters can later be generated from recorded reasons.
- Overrides become visible and attributable (`policy_band` vs `outcome` + `decided_by`).
- Real vendor model can drop in behind the stub interface; the reason-mapping fail-closed
  rule becomes its integration gate.

### Negative
- Deciding gets operationally stricter: reasons and drivers must exist or the decision is
  refused (fail closed) — a model/mapping bug now blocks decisions rather than issuing
  unexplained ones. That is the intended tradeoff.
- The ADR 0005 LLM package grows a tool-use surface (more code to maintain, FakeAdapter
  must script tool calls for offline tests).
- `decision_events` rides the shared-database seam (ADR 0002/0004): decision-service owns
  the write, but the schema is visible to all services.
- Sync latency/concurrency debt remains until the deferred async work is scheduled.
