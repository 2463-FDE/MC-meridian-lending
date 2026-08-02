# Spec: Auto-Generated Offer + TILA Disclosure (Week 4)

**Owner:** Dana (VP Lending Ops)
**Date:** 2026-08-01
**Status:** Built — D1–D9 implemented on `feature/disclosure-week4` (PR #12). All four design
decisions are settled (see *Open Decisions*, which records the reasoning and the rejected
alternatives). What remains outstanding is not a design decision but two **client answers** —
APR method of record and tolerance regime (see *Client Questions*); both can still change D1
and D3, which is why ADR 0012 stays *Proposed* rather than *Accepted*.
**Governs:** this spec governs implementation until ADR 0012 is accepted. ADR 0012 is written
*from* this spec (deliverable D8); where the two ever disagree, the spec is authoritative and
the ADR is the defect.
**Source brief:** Dana — "Once an app is approved, generating the offer and the disclosure
documents is still totally manual. Can you automate it — produce the offer and the TILA
disclosures right after approval? The numbers look basically right to me. The fee and APR
rules are scattered around the code a bit, but it works."
**Related:** ADR 0012 (this week, proposed), ADR 0002 (single shared DB), ADR 0005 (LLM
client), ADR 0006 (logging redaction), ADR 0008/0009 (decision events, append-only),
ADR 0010 (ownership authz), ADR 0011 (mandatory KYC), Reg Z / 12 CFR 1026 App. J.

---

## Executive Summary

Dana asked for automation and said the numbers look basically right. **They are not.** The
disclosed APR is computed with a crude add-on annualization instead of the Reg Z actuarial
method, and it is wrong on every loan by roughly 36× the regulatory tolerance. Automating
the current pipeline would mass-produce a legally defective disclosure faster.

This week therefore delivers **correctness first, automation second**:

1. Decimal actuarial APR + finance charge (the LLM never computes a regulated number).
2. One externalized fee rate, replacing three drifted hardcoded copies.
3. A knowledge graph over `applicant → application → decision_event → offer → disclosure`
   closing the provenance gap, as FK-as-graph in the existing Postgres.
4. A coordinated maker-checker multi-agent assembly pipeline that formats the document from
   numbers it is not allowed to compute, gated by a deterministic recompute + tolerance check.
5. TILA test vectors as a blocking CI gate.

## Minimum Build Slice

The critical path, in dependency order. Everything below this list is either detail on one of
these steps or a safeguard that only makes sense once the step it protects exists. Anyone
picking this up should build in exactly this order — each step is landable and testable on its
own, and each one is a prerequisite for the next.

1. **Externalized fee loader, fail-closed** (D2). Everything downstream reads a fee; until
   there is one authoritative rate the APR cannot be verified against anything. Fails closed
   with no default — a silently defaulted fee moves a regulated number.
   → `a865a4a`
2. **Decimal actuarial APR + finance charge, using that loaded fee** (D1). The headline
   defect. Depends on step 1 for its input.
   → `0df7c76`
3. **TILA vectors as a blocking CI job** (D7). Immediately after the math, not at the end of
   the week: the original defect survived because the money test ran under `|| true`, so the
   gate is part of the fix rather than a report on it. Expectations are pinned literals from
   an independent solve — never regenerated from `compute_apr`.
   → `faad1b8`, `97b08d8`
4. **`disclosures` table + `offers.decision_event_id` + provenance view** (D3). The record the
   pipeline writes into and the edge that makes it traceable. Needs the math settled first,
   because the row stores a `compute_snapshot` of the inputs the figures came from.
   → `5b567d6`, `8c468dd`
5. **Deterministic verify gate before persistence** (D4/D5). Recompute-and-compare in plain
   Python, owning every pass/fail. The assembly agents may only run behind it. Do not wire an
   LLM into this flow before the gate exists — the gate is the control, the agents are the
   convenience.
   → `702c1fe`, `5ed1429`, `b386bff`
6. **Thin frontend action + status, last** (D9). A button that triggers a pipeline whose
   numbers are not yet verified is a liability, not a demo.
   → `371e976`, `8ca880e`

Steps 3 and 4 landed in that order because the gate protects the math, not the schema; the
dependency runs 1 → 2 → {3, 4} → 5 → 6, with 3 and 4 independent of each other.

Deliverables D6 (compliance hold / delivery lifecycle) and D8 (this ADR) attach to steps 5 and
6 and are not on the critical path.

## Problem Statement

### Verified in code, 2026-08-01

**Q1 — The APR method is wrong. This is the headline.**
`services/disclosure-service/app/apr.py::compute_apr` computes
`(finance_charge / amount_financed) / years * 100` — an add-on annualization that spreads the
finance charge over the *full initial* balance. An installment loan amortizes, so the true
rate is roughly double.

Re-verified against the module for the docstring's loan (principal 18000, 7.99%, 48 months):

| Value | Result |
|---|---|
| `compute_apr()` as shipped | **5.041%** |
| Actuarial APR (policy 3.0% fee, amount financed 17460) | **9.584%** |
| Delta | **4.543pp** |
| Reg Z regular-transaction tolerance | 0.125pp |
| Breach factor | **~36×** |

The disclosed APR (5.041%) prints **below the 7.99% note rate** — arithmetically impossible
for a loan with an origination fee, and the tell that the method, not the rounding, is broken.

*The docstring's own worked example (float 7.142% / Decimal 7.157%) reproduces from neither
the shipped code nor the actuarial method. It is illustrative. Do not cite it.* Float rounding
is a real but secondary defect (~0.015pp); fixing rounding alone leaves the 4.5pp error intact.

**Q2 — The origination fee has drifted across three copies, and one disclosure carries two of them.**

| Location | Value |
|---|---|
| `policies/fee_schedule.md` (published source of truth) | 3.0% |
| `app/apr.py:13` `ORIGINATION_FEE_PCT` | 0.025 |
| `app/fees.py:7` `ORIGINATION_FEE_PCT` | 0.030 |
| `app/offer.py:8` `ORIGINATION_FEE_PCT` | 0.03 |

`offer.build_offer(18000, 7.99, 48)` returns `amount_financed = 17460.0` (fee $540 @ 3.0%)
while the `apr` field in the same dict was computed from a $450 fee (2.5%). A single
disclosure contradicts itself.

**Q3 — The rounded float is what ships.** `compute_apr` returns `round(apr, 3)` and no exact
value exists anywhere. That rounded number is the one with TILA legal weight. Rounding is not
the defect; the value being rounded is.

**Q4 — No provenance link.** `offers` (db/init/001_schema.sql:76) has `app_id` only — no
`decision_event_id`, no captured inputs, no ruleset version, no content fingerprint. No
`disclosures` table exists at all. Given a disclosure, you cannot prove which decision, which
inputs, or which rule versions produced it. This is the gap the knowledge graph closes.

### Why "basically right" is the dangerous part

The defect is silent (no crash; the `backend` CI matrix runs money tests with
`continue-on-error` + `|| true`), plausible (5% reads like a normal consumer APR), and ships
on every loan. The client's confidence is the risk, not the arithmetic.

## Deliverables (In Scope)

### D1. Decimal actuarial APR + finance charge

Replace the add-on approximation in `app/apr.py` with the Reg Z actuarial method (App. J):
solve for the periodic rate that discounts the payment stream to the amount financed
(bisection or Newton), in `Decimal`, not float. `app/schedule.py` amortization moves to
`Decimal` on the same path.

**Acceptance:**
1. `compute_apr(18000, 7.99, 48)` returns 9.584% ± 0.001 with the policy 3.0% fee.
2. Disclosed APR ≥ note rate for every non-zero-fee loan (invariant test).
3. No `float` in the disclosure compute path; `Decimal` end to end.
4. The hardcoded `ORIGINATION_FEE_PCT` in `apr.py` is deleted, not corrected.

### D2. Externalized, versioned fee config with a fail-closed loader

One rate, loaded once. New versioned `policies/fee_schedule.*` machine-readable companion to
the existing markdown, plus `app/rules.py` that loads and validates it.

**Acceptance:**
1. `ORIGINATION_FEE_PCT` appears in zero Python modules.
2. Loader **fails closed** — missing, malformed, or out-of-range config raises at startup and
   the service reports unhealthy. It never falls back to a default rate. (Mirrors the existing
   pepper / internal-token posture.)
3. The loaded schedule carries a `fee_schedule_version` string, recorded on every disclosure.
4. A test asserts the value read at runtime equals `policies/fee_schedule.md`'s 3.0%.

### D3. Knowledge graph — provenance chain (FK-as-graph)

Nodes are existing rows; edges are foreign keys. **No new engine.** See *Storage Decision*.

| Node | Table | Change |
|---|---|---|
| Applicant | `applicants` | none |
| Application | `applications` | none |
| DecisionEvent | `decision_events` | none (already append-only, ADR 0009) |
| Offer | `offers` | **+ `decision_event_id` FK** |
| Disclosure | **new `disclosures`** | new table |

`decisions` is a mutable current-state pointer and is **not** a graph node; `decision_events`
is the system of record and is.

`disclosures` holds the authoritative record: money in integer minor units, APR as exact
`NUMERIC`, `compute_snapshot` JSONB (principal, rate, term, fee_pct), `fee_schedule_version`,
`apr_method_version`, `content_fingerprint` = hash(inputs + ruleset + outputs), status,
`delivered_at`, `offer_id` FK, `decision_event_id` FK.

Traversal `disclosure → offer → decision_event → application → applicant` is exposed as a
read-only view. The multi-agent pipeline reads provenance **through that view**, not via
ad-hoc joins scattered across call sites — "pulls from the KG" is a code-structure
requirement, not just a schema one.

**Acceptance:**
1. Migration `db/migrations/00xx_disclosures.sql` + canonical `db/init/001_schema.sql` updated
   (init is authoritative; migrations lag by convention — both must land).
2. The disclosure-service schema-readiness `/health` gate covers the new table, the new FK,
   and the new index, reporting `schema_not_ready:<object>` until present.
3. Given a disclosure id, one view query returns the full chain including the exact inputs and
   rule versions used.
4. `content_fingerprint` recomputes identically from the persisted snapshot.
5. `disclosures.decision_event_id` and `disclosures.offer_id` are `NOT NULL` — the table is new,
   so no legacy rows constrain it. `offers.decision_event_id` is nullable in the DDL but the
   write path refuses to create an offer without one (see *Backward Compatibility*).
6. On a volume that already holds `offers` rows, the migration applies cleanly, those rows keep
   their existing values, and the provenance view still returns them as a partial chain.

### D4. Multi-agent disclosure assembly — maker-checker on LangGraph

Stages 0–7 as LangGraph nodes; the typed-reason routing table becomes conditional edges.
See *Framework Decision*.

```
[0] GATE + ROUTE     authz (0010) · KYC (0011) · idempotency; route on decision outcome:
                       approve -> pipeline
                       refer   -> terminal exit: manual review, no disclosure
                       deny    -> terminal exit: adverse-action (Reg B) — roadmap stub
[1] GATHER + RULES   deterministic KG read + fee/APR-method config load (fail-closed)
[2] COMPUTE          Decimal actuarial APR / fee / finance charge / schedule — NO LLM
[3] ASSEMBLE (maker) LLM formats the document from given numbers; schema-validated
[4a] VERIFY GATE     deterministic recompute + tolerance + single-fee + provenance checks
[4b] NARRATE (checker) LLM frames the verdict on PASS; performs no math
[5] PERSIST + LINK   disclosures row (status=draft) + FK edges + fingerprint
[6] COMPLIANCE HOLD  human: draft -> in_review -> approved (reject routes by reason code)
[7] SIGN + DELIVER   officer sign (or auto per flag) -> deliver -> delivered_at + audit
```

**Invariant (carried from Week 3): the LLM never computes, adjusts, or approves a regulated
number.** Stage 2 is plain Python. Stage 4a is a deterministic system gate, never a node's
LLM judgment.

**One live path, one automated cycle.** Verified 2026-08-01: the policy bands emit exactly
`approve` / `refer` / `deny` (`services/decision-service/app/model_vendor.py:79-82`),
`decision.py:348` sets `outcome = band`, and `decision.py:118` refuses any system decision
whose outcome contradicts its band. `counteroffer` exists only as a schema comment
(`db/init/001_schema.sql:139`) and an LLM output enum
(`origination-service/app/llm/request_builder.py:74`,
`app/prompts/decision_assistant.py:36`); no code path emits it. There is no `conditional`,
`needs_docs`, or pending-conditions state anywhere in decision-service, the applications
router, or the schema.

The earlier draft of this pipeline routed stage 0 four ways. Three of those targets are
unreachable, so they are removed rather than built as dead branches. The Week 4 graph is one
live path (approve) plus terminal exits, containing exactly **one automated cycle** —
`4a → 3` on `render_mismatch`, bounded. Stage 6 rework is human-triggered re-entry, not an
automated loop.

**Noted defect (not fixed this week):** the decision-assistant prompt offers `counteroffer` as
a valid outcome enum while the band guard at `decision.py:118` would refuse it — the LLM can
propose a value the policy engine rejects. It fails closed, so it is not a live hazard, but
prompt and engine disagree on the outcome contract. Log it; fix it in whatever week
counteroffer becomes real.

**Acceptance:**
1. Every LLM call routes through the existing hardened `app/llm/client.py` (PII redaction,
   `guard_output`, cost guard, no-content LangSmith tracing). The framework's own transport is
   never the call path.
2. Both agents behind the `Coordinator` interface; native and LangGraph implementations both
   satisfy it, and the test suite runs against `FakeAdapter` with no real key.
3. Injecting a wrong number into the assembled document causes stage 4a to BLOCK.
4. Checkpointing is **off** (see *Security additions*).

### D5. Deterministic verification gate + bounded typed-reason routing

| Gate | Typed reason | Target |
|---|---|---|
| 4a | `render_mismatch` (numbers correct, text drifted) & attempt < N | Stage 3 (retry) |
| 4a | `tolerance_breach` / `number_wrong` / `provenance_incomplete` / `fee_rate_inconsistent` | BLOCK + flag |
| 6 | wording / formatting | Stage 3 (human-triggered re-entry) |
| 6 | wrong terms / rate / eligibility | terminal exit to decisioning |

Discriminator at 4a is numbers-first: deterministic tolerance + provenance checks run before
rendering checks, and only purely presentational failures may loop. Routing is by **typed
reason, never positional**; every loop carries a bounded attempt counter; retry exhaustion
escalates. No loop is ever placed around a deterministic gate.

**Acceptance:** unit tests for each row; a test asserting attempt exhaustion blocks rather
than loops.

### D6. Disclosure lifecycle, compliance hold, delivery timing

Status machine `draft → in_review → approved → delivered`, compliance reject routed by reason
code, `delivered_at` recorded, TILA timing check.

**Acceptance:** illegal transitions rejected; `delivered_at` set only on the delivery path.

### D7. TILA test vectors — blocking CI gate

New `tests/test_tila_vectors.py`: multiple loans varying principal, rate, term, and fee, each
asserting expected actuarial APR and finance charge within tolerance. Runs in a **blocking**
job, not the `|| true` matrix — a money-math regression must fail the build. This is the
control that makes Q1 non-recurring.

### D8. ADR 0012

`adr/0012-decimal-minor-units-and-externalized-rule-config.md` — Decimal/minor-units standard,
externalized rule config, TILA vectors as a blocking gate, the KG provenance model, the
framework reversal and its rationale, the storage decision and its named flip triggers.

### D9. Frontend (thin)

`app/underwriting/[appId]/page.tsx` — "Generate disclosure" action, status badge, compliance
approve/reject, deliver. Reuses the Week 3 assistant-panel patterns.

## Storage Decision — FK-as-graph

**Decision:** the knowledge graph is foreign keys in the existing Postgres. Node = row, edge =
FK, traversal = one view. No graph engine.

**Rationale:** the Week 4 traversal is fixed and shallow (5 hops, one JOIN). Apache AGE does
not read existing tables — it maintains its own graph namespace, so adopting it means
projecting and syncing a duplicate of the provenance chain, holding applicant PII in a second
store, maintained by application code that can drift from the money record. Introducing a
second copy of the audit chain, in the week whose thesis is that the audit chain is broken, is
the disease wearing the cure's clothes. FK-as-graph is also not a dead end: it is the source
data any engine would project *from*, so deferring costs no rework.

**Upgrade ladder and named triggers:**

| Rung | Adopt when |
|---|---|
| FK-as-graph *(now)* | — |
| `graph_edges` table (from, to, type, properties JSONB) | a second edge type appears, or an edge needs its own attributes (e.g. shared-SSN link with confidence + observed-at) |
| Apache AGE | entity resolution exists **and** a committed link-analysis requirement exists (fraud/AML rings, variable-depth traversal) |
| Neo4j | graph workload outgrows Postgres, or graph-native RBAC/algorithms are required |

Note for the AGE rung: `docker-compose.yml:5` pins `postgres:16-alpine`, which carries no AGE
extension. Adopting it means changing the base image of the one database all seven services
share (ADR 0002), and AGE's supported-Postgres matrix must be verified as current at that
time — not assumed.

**Deliberately deferred, not overlooked:** the fraud/AML use case's hard problem is entity
resolution (proving two applicant rows are one person), not traversal. Buying a traversal
engine before the linking logic exists is buying the last mile first.

## Framework Decision — LangGraph, adopted for a non-engineering reason

**Decision:** LangGraph orchestrates stages 0–7, contained to the orchestration layer.

**Rationale, stated plainly because the ADR must survive being asked:** on engineering merit
native won, and that analysis has not changed — the defect is a wrong formula, fee drift, and
missing provenance, and a framework mitigates none of it. Adopt LangGraph and change nothing
else and the APR still prints 5.041%. Native was locked on 2026-07-23 while the deciding input
— whether framework adoption is graded — was still unanswered. It was answered on 2026-08-01:
**it is graded.** The conditional resolves to LangGraph. It buys the defect nothing; it buys
the deliverable. Both halves belong in the ADR.

Adoption is bounded by a `Coordinator` interface with two implementations, so reversal is
cheap. LangGraph is orchestration only — it has no relationship to the storage decision above.
Its graph is control flow held in memory for one run; the KG is business entities on disk. The
word collides; the things do not touch.

**Security additions this adoption makes mandatory:**

1. Every LLM call goes through `app/llm/client.py`. The framework never owns the transport.
2. **Callback surface audit** — LangGraph callbacks can carry raw prompt/response past
   `guard_output`, and the `redactor-drift` / `redaction-tests` gates do not cover framework
   internals. An explicit test must prove graph state and callbacks are redacted.
3. **Checkpointing off for Week 4**, decided explicitly rather than left at its default.
   Persisted graph state holds applicant PII at rest; the pipeline is a single synchronous run
   and needs no durable resume. Revisit trigger: any asynchronous re-entry into the pipeline —
   a stipulation/document-collection state or a counteroffer round-trip, neither of which
   exists today (see D4). If it
   is ever enabled it needs a redaction + retention review and its own blocking CI gate.
4. Dependency pins (`langgraph`, `langchain-core`, pydantic) enter **origination-service's**
   `requirements.txt` only — blast radius one service, checked against the existing
   `anthropic` / `langsmith` pins.

## Client Questions (Dana — decision-changing, ask before compute code locks)

1. **APR method system of record** — the Reg Z actuarial method (App. J), or a documented
   Meridian variant? This spec assumes actuarial. Everything in D1 and D7 keys off the answer.
2. **Tolerance regime** — regular-transaction 0.125pp or irregular 0.25pp? Sets the line the
   stage 4a gate enforces and the assertion in every TILA vector.
3. **Back-book scope** — fix forward, or quantify and cure disclosures already delivered? This
   spec fixes forward (see *Backward Compatibility* rule 3). The same answer settles whether
   the 2.5%-fee APR path ever reached a borrower, which is a disclosure-remediation question,
   not an engineering one.

*Not decision-changing, already settled:* the fee value is 3.0% per `policies/fee_schedule.md`
and is externalized; "who signs" is a launch flag.

## Open Decisions

**All four are settled.** They are kept here with their reasoning and rejected alternatives so
ADR 0012 can be written once, from a record of what was actually weighed.

1. **Schema or engine — SETTLED: schema now, graduate to an engine later.** The client brief
   asks for "a knowledge-graph schema" and the alt-research card places Neo4j / AGE / Memgraph
   in the *research* space, so the deliverable is the schema. FK-as-graph satisfies it: nodes,
   edges, and a traversal the pipeline actually reads through. Graduation is not hand-waving —
   the ladder in *Storage Decision* names each rung and the condition that promotes it, and
   FK-as-graph is the source data any engine would project from, so nothing is thrown away. If
   a running engine is ever required for demonstration rather than for traversal, the answer is
   AGE as a **read-only projection built from the FK chain** — never a second system of record.

2. **`uq_offers_app` versus counteroffer provenance — SETTLED: leave the constraint alone.**
   `db/init/001_schema.sql:182` enforces `UNIQUE (app_id)` on `offers`, added by migration 0010
   to make offer generation **idempotent** — without it a double-click, browser retry, or
   gateway timeout after the downstream insert persists multiple regulated TILA records for one
   application. Multiple offers per application arise only from counteroffers, which the system
   cannot emit (evidence in D4). So `offers.decision_event_id` lands now as the provenance
   edge and the constraint stays untouched.

   **Required for this to remain cheap: every offer created from Week 4 onward carries a
   `decision_event_id`.** A future `UNIQUE (decision_event_id)` needs a populated column; nulls
   among *new* rows would turn a pure schema swap into a backfill against rows whose
   originating decision is ambiguous. But the column cannot be declared `NOT NULL` outright —
   see *Backward compatibility*: an initialized volume already holds `offers` rows with no such
   value, and `ADD COLUMN ... NOT NULL` would fail against them. The constraint is therefore
   enforced for new rows only, following the partial-index precedent migration 0010 set for
   legacy `app_id`-less rows.

   **Roadmap trigger:** *when the decision engine can emit `counteroffer`, offer cardinality
   moves from per-application to per-decision-event.* That change is: drop `uq_offers_app`, add
   `UNIQUE (decision_event_id)` + a `supersedes_offer_id` self-edge, and fix three read sites —
   `disclosure-service/app/routers/offers.py:112` and `:140` (`ORDER BY id LIMIT 1`, which would
   otherwise pin the borrower to the superseded original) and
   `origination-service/app/routers/applications.py:426` (`n_offers`, whose meaning changes once
   multiple offers per application are legal). Stage 0's terminal exit becomes a route-in: one
   edge. The compute, assemble, verify, persist, and disclosure model are untouched, because a
   counteroffer is a new `decision_event` and the pipeline is a pure function of one decision
   event. `supersedes_offer_id` is deliberately **not** added now — an unused nullable column
   with no defined semantics invites writes that mean nothing.

3. **Disclosure immutability — SETTLED: conditional freeze at delivery.** Unconditional
   append-only would contradict the status machine, since `draft → in_review → approved` are
   legitimate mutations. The trigger therefore blocks UPDATE/DELETE only when
   `OLD.status = 'delivered'`, mirroring the `decision_events` pattern at
   `db/init/001_schema.sql:152`, plus a statement-level trigger for TRUNCATE (row triggers do
   not fire on it, and truncation would erase delivered rows wholesale). Table and triggers
   ship in the same migration, so no window exists in which delivered rows are mutable.
   Compliance may ratify the posture afterward; ratification does not change the DDL.

   *Amended at implementation:* the earlier draft of this line had re-issue creating a new row
   with a `supersedes_disclosure_id` column, shipping now. That column is **not** built. Re-issue
   only exists once delivery is a real channel (transport is stubbed this week), so shipping the
   pointer now would be precisely what Open Decision 2 declines to do for `supersedes_offer_id`
   — an unused nullable column with no defined semantics, inviting writes that mean nothing.
   Instead `uq_disclosures_offer` enforces one disclosure per offer, giving the same idempotency
   guarantee `uq_offers_app` gives the offer. **Roadmap trigger:** *when a real delivery channel
   lands, re-issue supersedes rather than replaces, and the uniqueness moves off bare `offer_id`.*

4. **Ruleset — SETTLED: property, not node.** Version strings (`fee_schedule_version`,
   `apr_method_version`) on the disclosure row, plus a versioned committed policy file. Content
   is recoverable from git by version; integrity is covered by `content_fingerprint`. A
   `rulesets` table is purely additive later and backfillable from those strings. Recorded
   counter-argument: an auditor asking "show me the rules as of this disclosure" gets a git
   checkout, not a query. Accepted for now — and that request is the trigger to add the table.

## Backward Compatibility

There is no `disclosures` table yet, so nothing in it can break. The compatibility surface is
the **`offers` rows that already exist** — today they *are* the TILA/Reg-Z record, and on any
initialized volume they were produced by the defective method described in Q1/Q2. Five rules:

1. **The new column is nullable in the DDL; the invariant is enforced for new rows only.**
   `ADD COLUMN decision_event_id INTEGER NOT NULL` fails outright on a table that already has
   rows. So the column is nullable, the write path refuses to create an offer without one, and
   the future `UNIQUE (decision_event_id)` is a **partial** index
   (`WHERE decision_event_id IS NOT NULL`) — the same pattern `uq_offers_app` already uses for
   legacy `app_id`-less rows (`db/init/001_schema.sql:182`, migration 0010's "Partial so any
   legacy app_id-less offer row is unaffected"). Legacy rows stay legal and untouched.
2. **Backfill is an optional, manual, operator-run step — never automatic.** Where an
   application has exactly one `decision_event`, the link is unambiguous and can be populated;
   where it has several, it cannot be inferred and must be left null rather than guessed. This
   mirrors migration 0010's manual dedup posture: a script that writes provenance it cannot
   prove is worse than a null. Provide the inspect query, not an auto-run `UPDATE`.
3. **Existing offers keep their existing numbers. They are not recomputed.** The value that was
   disclosed to the borrower is the legally operative one; silently overwriting a shipped 5.041%
   with 9.584% would destroy the evidence of what was actually disclosed and manufacture a
   record that never existed. Legacy rows carry no `apr_method_version`, and absence means
   "computed under the pre-Week-4 add-on method." **Whether the back book gets quantified and
   cured is Dana's call, not an implementation detail** — see *Client Questions*. This spec
   fixes forward only.
4. **The provenance view LEFT JOINs.** A legacy offer with no `decision_event_id` and no
   disclosure must still resolve to a partial chain rather than disappear from the view. A
   provenance query that silently omits the rows with the worst provenance would be the exact
   inversion of the point.
5. **Existing read paths keep working unchanged.** `GET /applications/{id}/offer` continues to
   serve legacy offers; nothing requires a disclosure to exist for an offer that predates this
   week. The disclosure-service `/health` readiness gate will report
   `schema_not_ready:<object>` until the new objects exist — loud and intended, matching the
   `uq_offers_app` precedent — but that is a deployment-ordering signal, not a data break.

## Prerequisites (from `docs/debt-log.md`)

The debt log carries four entries (D1, D2, D5, D13). Only one intersects this week.

**D2 — Float arithmetic for money. Directly in scope, partially.** D2's location list already
names `apr.py`, `fees.py`, `offer.py` as float calculation sites and `db/init/001_schema.sql`
lines 68–72 as the float `apr` / `monthly_payment` / `finance_charge` columns. Its stated
mitigation path is a platform-wide migration to `NUMERIC(19,2)` plus a recalculation of all
outstanding balances. **This week deliberately takes a beachhead, not the platform.** The
`disclosures` table is minor-unit/NUMERIC and authoritative; everything else stays float.

Two consequences that must be written down now rather than discovered later:

1. **A lossy boundary is being created on purpose.** The correctly computed `Decimal` values
   are written to `disclosures` exactly and to the existing `offers` `DOUBLE PRECISION`
   columns as a rounded copy. Where the two disagree, `disclosures` is authoritative. Any
   reader treating `offers.apr` as the disclosed value is reading a convenience copy. This
   must be stated in the ADR and in a column comment on the migration, or the next person
   reasonably assumes `offers` is the record.
2. **D2's status line must be updated, not left stale.** After this week D2 becomes "partially
   mitigated — disclosure compute path is Decimal/minor-units; intake, decisioning, servicing,
   and payments remain float." Leaving D2 marked plainly Open understates the change; marking
   it Closed would be false.

**D2's test note is a live hazard for D7.** `services/servicing-service/tests/test_money.py`
contains tests that fail by design and are tolerated because the `backend` job runs with
`continue-on-error` + `|| true`. The new TILA vectors must **not** land in that matrix — a
money-math gate that inherits `|| true` proves nothing. D7 gets its own blocking job.

**D1 / D5 / D13 — not blocking, do not regress.** No credential, logging, or card-data surface
is touched. The redaction controls (`redactor-drift`, `redaction-tests`) are, however, in the
blast radius via the framework's callback surface — see D4 acceptance #1 and the callback audit.

**Nothing in the debt log blocks starting.** No prerequisite remediation is required before D1
or D2 of this spec begin.

## Dependencies

### Existing code this builds on (verified present)

| Dependency | Location | Used by |
|---|---|---|
| Hardened LLM client | `services/origination-service/app/llm/` (client, adapter, transport, validator, request_builder) — `app/llm_client.py` is a compat shim re-exporting it, not a second implementation | D4 stages 3, 4b |
| `FakeAdapter` injection | same package | all agent tests, no real key |
| Officer-OR-owner authz | `origination-service/app/authz.py::require_officer_or_owner` | D4 stage 0 |
| KYC gate | `origination-service/app/kyc_gate.py::require_kyc_passed` | D4 stage 0 |
| Downstream HTTP clients | `origination-service/app/clients.py` | D4 stage 1 |
| Offer entry point | `origination-service/app/routers/offers.py::make_offer` (already wires authz + KYC) | D4 entry |
| Internal-only caller gate | `disclosure-service/app/routers/offers.py::_require_internal_caller` (`X-Internal-Service`) | D3, D6 new routes |
| Idempotent persistence pattern | same module (`create_offer`) | D3 persistence |
| Append-only precedent | `db/init/001_schema.sql:152` trigger on `decision_events` | Open decision 3 |
| Published fee source of truth | `policies/fee_schedule.md` (origination fee 3.0%) | D2 |

### Blocking CI gates in the blast radius

`offer-guard-gate` is the one to respect most: its stated premise is that the disclosure-service
offer surface is internal-only and reachable **only** through origination's officer-OR-owner
routes. New disclosure write routes sit on that same anonymous `/disclosure` proxy path, so
they must adopt `_require_internal_caller` and extend `test_auth_gate.py`, or the gate's
premise silently stops being true while the job stays green.

Also touched: `db-readiness-gate` and `decision-db-readiness-gate` (new schema objects must be
reported by `/health` as `schema_not_ready:<object>` until present — follow the `uq_offers_app`
precedent at `db/init/001_schema.sql:178`), `adr-0010-authz-gate`, `redactor-drift`,
`redaction-tests`. One new blocking job for D7.

### External dependency risk

`langgraph` + `langchain-core` enter `origination-service/requirements.txt`, which already pins
`anthropic`. This repo has been bitten here before — commit `011f296` on
`feature/llm-foundation-week1` reads *"pin anthropic to 0.116.0 (>=1.0.0 was unresolvable — no
such release)"*. **Resolve the pin set in an isolated install before writing any node code.**
If `langgraph` cannot coexist with the current `anthropic` / `langsmith` pins, that is a
Day-1 discovery, not a Day-4 one, and the `Coordinator` seam means the native implementation
remains a viable fallback.

### Ordering — what genuinely blocks what

```
D2 (fee config + fail-closed loader) ──> D1 (Decimal APR) ──> D7 (TILA vectors)
                                              │
                                              └──> D3 (schema + KG) ──> D5 ──> D6 ──> D9
                                                        ▲
LangGraph pin resolution ─────────────────────────> D4 ─┘
```

- **D2 before D1.** The actuarial computation needs one authoritative fee rate; doing D1 first
  means writing the new formula against a rate that is about to move.
- **D1 before D7.** Vectors assert against the corrected method.
- **No decision gates the migration any more.** Decisions 2–4 are settled (constraint unchanged,
  conditional freeze at delivery, ruleset as property), so D3's DDL shape is determined. This
  was the plan's only hard gate and it is closed.
- **Decision 1 (schema vs engine) blocks nothing** — an AGE projection, if ever required, is
  built *from* the FK chain and can be added later without altering it.
- **Client questions 1 and 2 should be answered before D1 locks.** The spec proceeds on
  actuarial + 0.125pp; a different answer changes the formula and every vector, which is cheap
  to change now and expensive after D7 exists.
- **Parallelizable:** D2+D1 (disclosure-service) and the LangGraph pin spike (origination) can
  run at the same time; they share no files.

### Scope lock

The intent is that ADR 0012 is written **once, from this spec, after all four decisions are
pinned** — not drafted early and amended later. That is why the decisions above are recorded
here with their rationale and their rejected alternatives: the spec is the working document
and absorbs the churn; the ADR is the settled record. If a decision changes after the ADR
lands, it gets a superseding ADR, not an edit.

## Out of Scope (Roadmap)

- Adverse-action Reg B notice document (reuses Week 3 `principal_reasons`) — stage 0 routes to
  a stub.
- **Counteroffer.** Not deferred for effort — the decision engine cannot emit the outcome
  (evidence in D4), so there is nothing to route. Building it means new band logic, a policy for
  what terms to counter with, and the Reg B notice that an unaccepted counteroffer can require:
  a decision-service week, not a disclosure one. Re-entry costs one edge when it lands, because
  a counteroffer is a new `decision_event` and the pipeline is a pure function of one decision
  event. Schema consequences and the three read sites to fix are recorded in Open Decision 2.
- Document collection / stipulation UI (upload, clear, re-trigger) — likewise unreachable: no
  `conditional`, `needs_docs`, or pending state exists in the decision engine or the schema.
- Compliance queue UI, assignment, SLA.
- Real delivery channel (email / mail / e-sign) — `delivered_at` is recorded, transport is stubbed.
- `offers` money columns to integer minor units. `disclosures` is authoritative in minor units
  this week; `offers` keeps its `DOUBLE PRECISION` columns and receives the correctly computed
  value. The Q1 sin was the formula, not the column type.
- Decimal conversion for the rest of the platform (intake, servicing, payments) — remains D2
  debt in `docs/debt-log.md`.
- A configurable rules engine. One externalized fee schedule is this week's scope.
- Any graph engine (see ladder).

## Acceptance Criteria (End of Week)

### Functional
1. Approving an application generates an offer and a TILA disclosure without manual steps.
2. Disclosed APR matches the actuarial value within 0.125pp on every test vector.
3. One fee rate, sourced from config, consistent across APR and amount financed.
4. The provenance chain resolves in one query from disclosure back to applicant.
5. Compliance hold blocks delivery; approve then delivers and stamps `delivered_at`.

### Security / Compliance
6. No LLM output reaches a regulated number. Injected-wrong-number test BLOCKs at 4a.
7. Redaction gates stay green, extended to cover the framework's callback surface.
8. Rules loader fails closed; disclosure-service reports unhealthy on bad config.
9. Checkpointing off; verified by test, not by convention.
10. No PII added to any new store or trace.

### Process
11. TILA vectors run in a blocking CI job.
12. ADR 0012 merged with the framework and storage decisions recorded, rationale included.
13. `make prove` passes for each regression test (fails without the fix, passes with it).
14. `teeth` adversarial pass before PR.
15. `docs/kb.md`, `docs/debt-log.md`, and the feature status tracker updated.

## Verification

- **TILA vectors** (blocking): multi-loan actuarial expectations with tolerance asserts.
- **Unit**: Decimal APR; rules loader fail-closed; disclosures persistence; status machine;
  coordinator routing; bounded retry; verify-fail → block; `render_mismatch` → retry.
- **Integration**: endpoint plus agent loop, `FakeAdapter`, no real key.
- **Smoke vs live compose**: approve → generate → disclosure persisted as draft, KG edges
  present, fingerprint set; inject a wrong number → gate BLOCKs; compliance approve →
  `delivered_at` recorded.
