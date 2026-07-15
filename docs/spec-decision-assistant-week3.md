# Spec: Single-Agent Decisioning Assistant (Week 3)

**Owner:** Dana (VP Lending Ops)
**Date:** 2026-07-15
**Status:** Approved for build on the feature branch. Sign-off state per ADR 0008's four-owner gate: product approved (Dana's directive), engineering approved (ADR 0009); compliance/legal and data-owner reviews **pending** — both gate production rollout, not branch implementation. See ADR 0009 *Sign-off status*.
**Source brief:** Dana's request — "wrap the new AI credit-scoring model in an assistant that decisions an application and tells the officer the result."
**Related:** ADR 0008 (decision-record field contract, locked 2026-07-07), ADR 0005 (LLM client), ADR 0007 (identifier-free projection), CFPB Circular 2023-03.

---

## Executive Summary

Build a single-agent decisioning assistant: an LLM agent (score tool + decision memory) that
decisions a loan application and reports the result to the loan officer — while fixing the
compliance hole underneath it. Every decision persists an **append-only decision-event
record** (inputs, model outputs, reason codes) per the field contract ADR 0008 already
locked. Adverse-action reasons become **specific and accurate** — derived from the model's
actual top features, not the generic "purchasing history" string.

**The "licensed AI model" is hypothetical.** No vendor artifact exists. This spec defines a
deterministic stand-in module that emits the vendor output shape (score + ranked feature
attributions + model id/version) behind an interface a real model can later replace. The
agent is the deliverable; the model is a stub.

## Problem Statement

**Surface request (Dana):** wrap the new, more accurate model in an assistant; move fast;
the board is watching.

**Real problem (Reg B / CFPB Circular 2023-03 — no AI exemption):**
- Every denial today carries the same generic reason: `GENERIC_REASONS[0]` = "purchasing
  history" (`services/decision-service/app/decision.py`), a nearest-checkbox that reflects
  nothing the model weighed.
- No decision record is persisted beyond `(app_id, outcome)` — no inputs, no drivers, no
  reason, no timestamp. A disputed denial is unprovable (denials #6012/#6013 have no
  recorded reason anywhere).
- #6012 scored 612 (refer band per underwriting guidelines) yet was recorded as deny — an
  invisible, unattributed override.
- Model accuracy does not cure any of this: an accurate model with a wrong or unprovable
  stated reason is the compliance failure.

**Known, deferred:** the synchronous decisioning chain times out past ~20 concurrent
applications. This week documents the sync→async path in the ADR; it does not build it.

## Deliverables (In Scope)

### D1. Hypothetical vendor model stub
A deterministic module in decision-service standing in for the licensed model:
- Emits the vendor output shape: `score`, ranked `feature_attributions` (signed
  contributions per feature), and a `model_id`/`model_version` string.
- Deterministic for a given input (testable, reproducible).
- Behind a small interface so a real vendor model can replace it without touching the
  write path.

**Acceptance:**
1. Module exists in `services/decision-service/app/` with a deterministic `score()` (or
   equivalent) returning score + ranked feature attributions + model id/version.
2. Same input → same output (unit-tested).
3. The write path consumes only the interface, not stub internals.

### D2. Feature → adverse-action reason mapping
- A mapping from model features to specific Reg B principal-reason codes (adverse-action
  vocabulary), locked in the ADR (D5).
- Deny/refer outcomes MUST carry ≥1 specific principal reason derived from the model's
  actual top negative features. The generic "purchasing history" string MUST be removed.
- Every feature the stub model can emit has a mapped reason (no unmappable feature).

**Acceptance:**
1. Mapper module unit-tested: every model feature maps to a specific reason code + text.
2. Deny/refer decisions produce reasons traceable to the top negative attributions for
   that applicant (different drivers → different reasons).
3. "purchasing history" appears nowhere in the decision path.

### D3. Decision memory — append-only decision-event record
Per ADR 0008's locked field contract:
- New `decision_events` table (additive migration in `db/migrations/` +
  `db/init/001_schema.sql`): `app_id`, `outcome`, `principal_reasons`, `drivers` (model
  score, top attributions, band cutoff applied), `policy_band`, `inputs`
  (identifier-free: no SSN/name/PAN/DOB/address — ADR 0007 rule 1), `decided_by`
  (model id+version, or user id for overrides), `decided_at`.
- **Append-only:** the code path has no UPDATE/DELETE on this table.
- **Write-path rule (ADR 0008 req. 2):** no outcome persists without `principal_reasons`
  + `drivers`. An outcome contradicting `policy_band` requires a human `decided_by` —
  the system cannot silently override (fixes the #6012 class).
- The existing `decisions` upsert remains as the current-state pointer; `decision_events`
  is the system of record.

**Acceptance:**
1. Migration adds `decision_events`; DDL in both `db/migrations/0004_*.sql` and
   `db/init/001_schema.sql`.
2. Every decision issued writes one event row atomically with the outcome.
3. Unit test: write path refuses an outcome without reasons/drivers; refuses a system
   outcome that contradicts the policy band.
4. `inputs`/`drivers` stored identifier-free (no SSN etc.) — tested.
5. A decision record is retrievable by `app_id` after the fact ("what proves why we
   denied?" is answerable from stored data).

### D4. Single-agent decisioning assistant
An LLM agent (ADR 0005 `ClaudeClient`, extended with tool use) with two tools:
- **Score tool** — decisions the application via decision-service. The event record
  (D3) is persisted inside this call, atomically with the decision — the agent cannot
  decision an app without the record being written.
- **Decision memory tool** — retrieves the persisted decision record for an `app_id`
  (identifier-free projection).
- The LLM orchestrates tools and narrates the result to the officer, clean and simple:
  outcome, score, band, and the specific reasons. The LLM **never decides and never
  invents reasons** — outcome and reasons come only from tool results; the summary is
  generated from the recorded event.
- Anything sent to the LLM passes the existing redaction pipeline (ADR 0005/0006); tool
  results are identifier-free by construction.

**Acceptance:**
1. Officer-facing endpoint: given an `application_id`, the agent decisions the app and
   returns a plain-language result naming outcome + specific reasons.
2. Agent tool loop runs through `ClaudeClient` (no raw SDK calls outside ADR 0005).
3. Outcome/reasons in the response match the persisted event record exactly (LLM cannot
   contradict the record — validated, not trusted).
4. Asking about a previously decisioned app returns the recorded reasons via the memory
   tool (including "no record (legacy)" for pre-feature decisions like #6012, distinct
   from "not found").
5. Unit tests run offline via `FakeAdapter` (no API key, no network).
6. No PII/SSN/PAN in prompts, tool results, or logs — tested.

### D5. ADR
One new ADR (next number) that:
1. Locks the feature → adverse-action reason mapping table (D2).
2. Records the agent design: tools decide deterministically, LLM narrates; regulated
   write lives inside the score tool.
3. Contains the **sync→async note**: documents the synchronous chain's >20-concurrent
   timeout failure, sketches the async path (queue/event between origination and
   decisioning), and explicitly defers it.
4. Notes Dana's directive as product sign-off and the still-open compliance/legal review
   of Reg B citations (carried from ADR 0008).

## Out of Scope (Not This Week)

- Async decisioning implementation (documented in ADR only).
- Portal/UI surface for the assistant (API only).
- Adverse-action letter generation.
- Backfill of legacy decision rows (impossible — ADR 0008 req. 4; #6012/#6013 stay
  reason-less and the assistant must say so plainly).
- Real vendor model integration (stub interface is the seam).
- Migrating the `decisions` float/ORM debt beyond what D3 touches.

## Acceptance Criteria (End of Week)

### Functional
1. Agent endpoint decisions an application and reports outcome + specific reasons.
2. Every decision writes an append-only `decision_events` row (inputs, model outputs,
   reason codes, band, decided_by, decided_at).
3. Reasons are specific + accurate (top-attribution-derived); generic string removed.
4. Prior decisions retrievable with recorded reasons; legacy rows answered honestly.

### Security/Compliance
5. Write path enforces reasons+drivers presence and band-override rule.
6. Event `inputs`/`drivers` are identifier-free; nothing sent to the LLM contains
   PII/PAN/SSN; logs stay redacted (existing blocking CI jobs stay green).
7. LLM output validated against the persisted record before returning to the officer.

### Process
8. All work on `feature/decision-assistant-week3` off `main`.
9. Small commits tracing to spec sections.
10. ADR committed before implementation (D5 items 1–2 locked up front; sync→async note
    included).
11. Unit + integration tests pass; smoke test against the live stack (`make up`).
