# ADR 0012: Decimal Minor Units, Externalized Rule Config, and FK-as-Graph Provenance for TILA Disclosures

- **Status:** **Proposed** — built and merged to `main` (PR #12, `6b395cb`).
  Deliberately not *Accepted*: two client answers (APR method of record, tolerance regime) can
  still change D1/D3. Accept once Dana answers; if either answer differs from what shipped, it
  gets a superseding ADR, not an edit.
- **Date:** 2026-08-01
- **Author:** Claude Code
- **Related:** `docs/specs/disclosure-week4.md` — the source of truth, and it **governs
  implementation until this ADR is accepted**; this ADR is written from it (spec deliverable
  D8), so where the two disagree the spec is authoritative. ADR 0002 (single shared DB),
  ADR 0005 (LLM client guards), ADR 0006 (logging redaction), ADR 0008/0009 (append-only
  `decision_events`, LLM-never-scores), ADR 0010 (ownership authz), ADR 0011 (mandatory KYC),
  debt D2 (float money), Reg Z / 12 CFR 1026 App. J

---

## Context

**The business problem.** Meridian discloses a legally wrong APR on every loan it originates,
and cannot prove which decision produced any disclosure it has issued. Reg Z puts a *tolerance*
on the disclosed APR: exceeding it is a violation, not a cosmetic defect. The exposure is per
loan and it is accruing now.

Dana asked for something narrower — automate offer and disclosure generation after approval —
and stated the numbers "look basically right." That assumption is the risk, and it does not
survive contact with the code. Verified 2026-08-01:

| Finding | Evidence |
|---|---|
| APR uses add-on annualization, not the Reg Z actuarial method | `apr.py::compute_apr` = `(finance_charge / amount_financed) / years`; spreads the charge over the full initial balance while the loan amortizes |
| For the module's own worked loan (18000, 7.99%, 48mo): **5.041%** disclosed vs **9.584%** actuarial | 4.543pp against a 0.125pp tolerance ≈ 36×; disclosed APR prints *below* the 7.99% note rate, impossible with an origination fee |
| Origination fee hardcoded three times and drifted | `apr.py:13`=0.025, `fees.py:7`=0.030, `offer.py:8`=0.03 vs 3.0% in `policies/fee_schedule.md` |
| One disclosure carries two fee rates | `offer.build_offer` returns `amount_financed` from a $540 fee beside an `apr` from a $450 fee |
| No provenance | `offers` (`001_schema.sql:76`) has `app_id` only — no decision link, inputs, rule versions, or fingerprint; no `disclosures` table exists |

The defect is silent (the `backend` matrix runs money tests under `continue-on-error` +
`|| true`), plausible (5% reads like a normal consumer APR), and universal. **Automating this
pipeline unchanged would mass-produce a defective legal document faster.** Correctness is
therefore the deliverable and automation is the vehicle.

Two further assumptions were challenged rather than inherited: that a *knowledge graph* implies
a graph database (it does not — the brief asks for a schema), and that float **rounding** is the
TILA problem (it contributes ~0.015pp against a 4.543pp method error).

## Decision

**D1 — `Decimal`, minor units, actuarial APR, on the disclosure path only.** `compute_apr`
becomes an actuarial solve on the payment stream; `schedule.py` amortization follows. The new
`disclosures` table stores money as integer minor units and APR as exact `NUMERIC`. Intake,
decisioning, servicing, and payments stay float — debt D2 becomes *partially mitigated*, not
closed. Values are written to `disclosures` exactly and to the existing `offers`
`DOUBLE PRECISION` columns as a **rounded copy**; `disclosures` is authoritative and the
migration must say so in a column comment.

**D2 — One externalized, versioned fee schedule behind a fail-closed loader.** All three
hardcoded constants are **deleted, not corrected**. `app/rules.py` loads and validates a
versioned companion to `policies/fee_schedule.md`; missing, malformed, or out-of-range config
raises at startup and the service reports unhealthy, never falling back to a default. Each
disclosure records its `fee_schedule_version`.

**D3 — TILA vectors run in a blocking CI job.** No `continue-on-error`, no `|| true`.
`servicing-service/tests/test_money.py` already contains tests that fail by design and are
tolerated; a money gate inheriting that tolerance proves nothing.

**D4 — The knowledge graph is foreign keys in the existing Postgres.** Node = row, edge = FK,
traversal = one read-only view. New `disclosures` node plus `offers.decision_event_id` close the
chain `applicant → application → decision_event → offer → disclosure`. `decision_events` is the
node (append-only, ADR 0009); `decisions` is a mutable pointer and is not. `disclosures` carries
`compute_snapshot` JSONB, `fee_schedule_version`, `apr_method_version`, and
`content_fingerprint` = hash(inputs + ruleset + outputs). The pipeline reads provenance through
the view, not via ad-hoc joins.

*Amended at review, 2026-08-04:* the node also carries `document_body` JSONB — the assembled
borrower-facing document, structured as the assembler produces it. The record previously held
what was **computed** and nothing of what was **disclosed**: the document existed only in the
generating call's HTTP response, so no later reader could see the artifact the delivered flag
referred to. Structured rather than a rendered blob, because the figures are then comparable
field by field against the same outputs the fingerprint covers, and disclosure-service refuses
any document whose figures disagree with its own recomputation. It is deliberately **outside**
`content_fingerprint`: that hash must recompute from the persisted snapshot alone, and folding
in model prose would make a regulated integrity value depend on wording while adding nothing —
the figures inside the document are already pinned to the hashed outputs. The document body is
read through its own officer-only route, not through the provenance view: the view is the one
definition of the chain, and origination's proxy of it admits the owning borrower, who must not
see a held draft's body.

*Amended at review, 2026-08-05:* `offers.decision_event_id` is written when the offer is
created, not when its disclosure is. Closing it at disclosure time meant taking whichever
`decision_events` row was latest by then, and that table is append-only: a re-decision between
the offer and its disclosure re-parented the offer to an event that did not authorize its terms,
and the view reported the result as a complete chain — a wrong edge reads as sound provenance,
where a missing edge reads as the partial chain it is. Creation is the only moment the
authorizing decision is known rather than inferred, because `uq_offers_app` means the offer is
never regenerated under the newer decision. `create_disclosure` requires the disclosed event to
equal the offer's and closes the edge itself only for an offer that carries none — a legacy row,
whose data supports nothing stronger than the same-application check.

**D5 — Multi-agent is maker-checker; the LLM never computes a regulated number.** Assemble
(maker) formats from given numbers; a deterministic **system** gate recomputes and checks
tolerance, fee consistency, and provenance; Narrate (checker) frames the verdict and does no
math. Routing is by typed failure reason with a bounded attempt counter — never positional,
never a loop around a deterministic gate. Only `render_mismatch` retries.

**D6 — LangGraph orchestrates, adopted for a non-engineering reason.** See *Options*. Bounded
by: every LLM call through the hardened `origination-service/app/llm/` client; a mandatory
callback-redaction audit; **checkpointing off**; pins confined to one service; both
implementations behind a `Coordinator` interface.

**D7 — Offer cardinality unchanged; delivered disclosures frozen; existing offers never
recomputed.** `uq_offers_app` stays (it is what makes generation idempotent under retry —
migration 0010). `offers.decision_event_id` is **nullable in DDL** because
`ADD COLUMN ... NOT NULL` fails on volumes holding rows; the invariant is enforced at the write
path and the future unique index is partial, following that same precedent. A trigger blocks
UPDATE/DELETE only when `OLD.status = 'delivered'`; earlier states mutate legitimately. Rule
versions are properties, not a `rulesets` node.

*Amended at review, 2026-08-04:* the delivery transition also requires a recorded document, so
`delivered` states that content exists and was readable, not only that a timestamp was written.
This is what the freeze then protects; before it, the trigger made immutable a row whose
document nobody could produce. `document_body` is written once, at insert — no lifecycle edge
touches it — so an officer cannot approve one document and deliver another. Rows written before
the column existed keep NULL and become undeliverable rather than being backfilled: inventing a
document at delivery time is fabricating the evidence the column exists to hold.

*Amended at review, 2026-08-05:* `document_body` is written once at insert OR by the idempotent
replay onto a row that has none — the one repair path, added because a row written before the
column existed otherwise had no route to a document and no route to delivery. It is still never
overwritten and no lifecycle edge touches it, so the approve-one-deliver-another case stays
closed. Every consumer of `delivered` re-checks the document rather than trusting the flag:
already-delivered rows keep NULL and are frozen against repair, so boarding refuses the pair of
`delivered` plus a NULL document instead of funding a loan whose disclosure cannot be read.

## Options Considered

### Money representation and rule config

| Option | Rejected because |
|---|---|
| Fix float rounding only | Rounding is ~0.015pp against a 4.543pp method error. Leaves the violation and creates the appearance of remediation — worse than visibly broken |
| Correct the three fee constants in place | Three synchronized copies drift again; that is exactly how this state arose |
| Platform-wide minor-unit migration now | Data migration plus recalculation of outstanding balances across servicing; would stall the actual fix behind months of work |
| **Chosen: Decimal/minor-unit beachhead on the disclosure path + externalized config** | — |

### Graph storage

| Option | Rejected because |
|---|---|
| Apache AGE | Does not read existing tables — maintains its own namespace, so the provenance chain must be projected and synced. A second copy of the audit chain, holding PII, maintained by code that can drift, in the week whose thesis is that the audit chain is broken. Also requires replacing `postgres:16-alpine` (`docker-compose.yml:5`), the base image of the DB all seven services share (ADR 0002) |
| Neo4j | Everything above plus a new vendor to certify, back up, and access-control; the Week 4 traversal is 5 fixed hops |
| `graph_edges` table | Solves edge *properties*, which nothing needs yet — no second edge type exists |
| **Chosen: FK-as-graph** | Also the source data any engine would project *from*, so deferring costs no rework |

Ladder and triggers, so deferral is a decision rather than an omission: `graph_edges` when an
edge needs its own attributes; **AGE** when entity resolution exists *and* a committed
link-analysis requirement exists (fraud/AML); **Neo4j** when the workload outgrows Postgres or
graph-native RBAC/algorithms are required. The fraud use case's hard problem is entity
resolution, not traversal — buying the traversal engine first buys the last mile first.

### Orchestration

| Option | Rejected because |
|---|---|
| Native coordinator | **Won on engineering merit and was the standing recommendation.** Rejected only when framework demonstration was confirmed graded (2026-08-01) |
| CrewAI | Routing is LLM judgment with no inspectable graph — unacceptable on a regulated path where an auditor asks why a step ran |
| Microsoft Agent Framework / AG2 | Azure/.NET ecosystem gravity against a Python/Postgres stack; AutoGen's retirement is a poor precedent for a multi-year engagement |
| **Chosen: LangGraph** | Inspectable control flow, typed state, cycles as explicit edges, same ecosystem as the LangSmith tracing already wired |

**Recorded plainly:** a framework mitigates none of the defect. Adopt LangGraph, change nothing
else, and the APR still prints 5.041%. Native was locked 2026-07-23 while the deciding input —
is adoption graded — was unanswered; it was answered on 2026-08-01. It buys the defect nothing;
it buys the deliverable. A future reader should know this was not decided on technical merit.

LangGraph is orchestration and has no bearing on the storage decision. Its graph is control flow
in memory for one run; the knowledge graph is entities on disk. The word collides; the things
do not touch.

### Counteroffer modelling

Rejected: a loop inside the pipeline. A counteroffer is new *terms*, which only decisioning may
produce; generating them inside the pipeline breaks the LLM-never-computes invariant at its
root. Chosen: a counteroffer is a new `decision_event`, and the pipeline is a pure function of
one decision event — so the path costs one edge later instead of reopening compute, verify, and
persist. It is unreachable today regardless: bands emit approve/refer/deny
(`model_vendor.py:79-82`) and `decision.py:118` refuses any outcome contradicting its band.

## Implementation Plan

| Phase | Work | Gate to proceed |
|---|---|---|
| 0 | Resolve `langgraph` + `langchain-core` pins against existing `anthropic`/`langsmith` (precedent: commit `011f296`, "pin anthropic to 0.116.0 (>=1.0.0 was unresolvable)"). Runs parallel to phase 1 | Pins resolve, or fall back to native via the `Coordinator` seam |
| 1 | D2: versioned fee config + fail-closed loader; delete three constants | Loader test green; service unhealthy on bad config |
| 2 | D1: Decimal actuarial APR + amortization | `compute_apr(18000, 7.99, 48)` = 9.584% ±0.001; APR ≥ note rate invariant |
| 3 | D3: TILA vectors in a new blocking job | Job blocks on a seeded regression |
| 4 | D4/D7: migration (`disclosures`, `offers.decision_event_id`, delivered-freeze trigger, `disclosures.document_body`) in **both** `db/init/001_schema.sql` and `db/migrations/` + `/health` readiness objects | Clean-volume and populated-volume apply both verified |
| 5 | D5/D6: coordinator, maker/checker nodes, typed-reason routing, callback-redaction test | Injected wrong number BLOCKs at the gate |
| 6 | Frontend action + status badge; `teeth` pass; docs/tracker update | — |

Sequencing rationale: D2 before D1 (the formula needs one authoritative rate), D1 before D3
(vectors assert the corrected method). Client questions 1–2 should be answered before phase 2
locks — cheap to change then, expensive once vectors exist.

**Both `db/init/001_schema.sql` and `db/migrations/` must land in the same change.** Compose
mounts `db/init/*` only, and commit `e0716da` is this exact bug: an index that lived only in a
migration left the replay path dead and `/health` permanently `schema_not_ready`.

*Amended at review, 2026-08-04:* adding a column to a table declared in both files takes three
edits, not two — the init DDL, the original migration's `CREATE TABLE` (a volume that never had
the table gets the column with it, and a test compares the two byte for byte), and a new
migration with `ADD COLUMN IF NOT EXISTS` for volumes where the original already ran. That
`IF NOT EXISTS` swallows a same-named column of any type, so the new migration compares
`data_type` and raises rather than skipping on the name, and the readiness rung probes the type
too. Without both, a `TEXT` stand-in for `JSONB` reports ready and hands every reader a string
instead of an object.

## Rollback Strategy

| Component | Rollback |
|---|---|
| D1/D2 compute | Pure code revert. No data written under the new formula is destroyed — `disclosures` rows remain readable and carry `apr_method_version`, so values stay attributable to the method that produced them |
| D4 schema | Additive only: one new table, two nullable columns (`offers.decision_event_id`, `disclosures.document_body`), one trigger. Rollback = drop trigger, drop table, drop both columns. No existing column is altered and no existing row is rewritten, so reverting cannot corrupt the back book. Dropping `document_body` re-opens the delivery gate rather than breaking it — the code revert that accompanies it removes the check |
| D6 LangGraph | Swap the `Coordinator` implementation back to native and drop the pins. This is the seam's entire purpose; it is the reason adoption is reversible at all |
| D3 blocking job | Delete the job. Reverting it is a policy regression, not a technical one — it should require the same review as adding it |
| Partial deploy | `/health` reports `schema_not_ready:<object>` until objects exist, so a code-ahead-of-migration deploy fails loudly rather than writing half-linked provenance |

## Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| LangGraph callbacks leak raw prompt/response past `guard_output`; `redactor-drift` / `redaction-tests` do not cover framework internals | High | Mandatory callback-redaction test before merge; every call routed through the hardened client |
| Checkpointer persists applicant PII at rest | High | Off, explicitly, and asserted by test rather than convention. Enabling requires a redaction + retention review and its own blocking gate |
| Two money representations (`disclosures` minor units, `offers` float) — someone reads the wrong one | Medium | Column comment on the migration, stated in D1, `disclosures` named authoritative in the view |
| Corrected APR applied retroactively to delivered disclosures | High | Explicitly prohibited: the value disclosed is legally operative. Absence of `apr_method_version` means pre-Week-4 method. Remediation is Dana's decision, not an implementation side effect |
| Backfilling `decision_event_id` guesses a link it cannot prove | Medium | Manual operator step, applied only where an application has exactly one decision event; otherwise left null. Mirrors migration 0010's manual dedup posture |
| Provenance view hides legacy rows lacking the new edges | Medium | LEFT JOIN — rows with the worst provenance must not be the ones that disappear |
| Disclosures written before `document_body` existed become undeliverable, with no officer action that gives them one (generation is disabled once a disclosure exists, and a replay returns the stored row rather than writing a document onto it) | Medium | Accepted, and the fail-closed direction: the alternative is delivering a document nobody can produce. The officer screen states it plainly and promises no self-service remedy, so the row surfaces as needing an operator instead of failing at the deliver click. Rows already `delivered` keep NULL and stay frozen — the trigger blocks any UPDATE of them, so no hand-backfill is possible either |
| A document whose figures agree in value but not in spelling reaches a borrower | Medium | The boundary check requires a plain-decimal literal before comparing: `Decimal` accepts `17_460.00`, `+3628.71` and `3.62871E+3`, all of which compare equal to the record, and the string is stored and printed verbatim |
| New disclosure routes on the anonymous `/disclosure` proxy bypass the officer-OR-owner path | High | Adopt `_require_internal_caller` and extend `test_auth_gate.py`; `offer-guard-gate`'s stated premise depends on it |
| Dependency conflict blocks phase 0 | Medium | Discovered Day 1 by design; native fallback preserved by the `Coordinator` seam |

## Cross-Cutting Impact

- **Security:** two new audit surfaces (framework callbacks, checkpoint store), both closed
  above. No new PII store; no change to card or SSN handling. *Amended at review, 2026-08-04:*
  `document_body` stores no new class of data — the figures already sit in adjacent columns and
  the prose fields are digit-free and identifier-free by the assembler's output schema — but it
  does add a read surface, closed by making the document route officer-only rather than the
  officer-or-owner posture the rest of the disclosure reads take.
- **Performance:** actuarial solve is an iterative root-find on ≤ a few hundred payments —
  microseconds, run once per disclosure. Framework overhead is negligible; token cost is driven
  by two agent calls, not by LangGraph.
- **Scalability:** unchanged. The pipeline inherits the existing synchronous coupling, which
  remains documented debt.
- **Reliability:** fail-closed config trades availability for correctness deliberately — a
  service that cannot prove its fee rate must not issue disclosures.
- **Maintainability:** one fee source replaces three; provenance reads go through one view. The
  `Coordinator` seam earns its keep with two real implementations rather than a speculative one.
- **Cost:** ~+2–3 days for framework adoption, dominated by security re-review, bought for the
  deliverable. Infrastructure cost is zero — no new engine, no new store.
- **Operational:** two migrations (0011, plus 0012 for the document column), `/health` readiness
  objects, optional manual backfill. No image change, no new service. Disclosures written before
  0012 need an operator: they cannot be delivered and no officer action gives them a document.
- **Testing:** one new blocking job; unit coverage for the Decimal path, loader, status machine,
  routing, and bounded retry; agent tests run on `FakeAdapter` with no key spend.

## Consequences

**Positive.** The disclosed APR becomes correct and a blocking gate keeps it correct. One
versioned fee rate removes the drift class rather than patching it. A disclosure is traceable to
the decision, inputs, and rule versions that produced it. The regulated number is deterministic
code on both the compute and verify sides, so the LLM cannot move it. Every deferral carries a
named trigger.

*Amended at review, 2026-08-04:* the record now holds what was disclosed as well as what was
computed, so `delivered` is a claim about content a human could read rather than a flag over an
artifact that lived only in one HTTP response.

**Negative.** Two money representations coexist across a seam that runs through the platform
rather than around it. Disclosures written before `document_body` existed are undeliverable and
need an operator — accepted as the fail-closed direction, but it is a manual queue this ADR
creates and does not staff. The back book keeps its defective numbers until Dana decides on
remediation — legally the correct default, and it means known-wrong disclosures stay on file.
LangGraph adds dependency weight and an audit surface bought for the deliverable, not the
defect. FK-as-graph cannot express edge properties until the `graph_edges` rung.

## What Could Supersede This

Client answers on the **APR method of record** (actuarial vs a documented Meridian variant) and
the **tolerance regime** (0.125pp regular vs 0.25pp irregular) change D1 and D3. The tolerance
is deliberately a config value in `policies/fee_schedule.json` rather than a literal, so that
answer costs a config change, not a rework. A decision to **quantify and cure the back book**
adds a workstream D7 currently declines.

The schema-versus-engine question is **decided: schema now, graduate later** — the deliverable
is a knowledge-graph schema, and D4's ladder names the condition that promotes each rung. A
later requirement for a running engine adds a rung (AGE as a read-only projection built from
the FK chain) rather than reversing this decision.

Per the scope lock in `docs/specs/disclosure-week4.md`: a decision that changes after this ADR is
accepted gets a superseding ADR, not an edit.
