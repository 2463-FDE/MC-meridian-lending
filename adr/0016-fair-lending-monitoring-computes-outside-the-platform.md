# ADR 0016: Fair-Lending Monitoring Computes Outside the Platform

- **Status:** **Proposed** — records the decision behind a spec that has merged. No export
  code is built.
- **Date:** 2026-08-13
- **Author:** Claude Code
- **Related:** ADR 0007 rule 1 (identifier-free projections — the rule `decision_events.inputs`
  is annotated with at `db/init/001_schema.sql:155`), ADR 0008 (retrievable decision records),
  ADR 0009 (the decisioning assistant that reads those records), ADR 0002 (single shared
  database — why "stored in this platform" means reachable by all seven services),
  ADR 0006 (logging redaction — the per-service PII redactor an export path sits beside).
- **Source:** `docs/specs/fair-lending-monitoring-week8.md` §4,
  `docs/model-card-decisioning-scorecard.md` §7, `docs/cards-week8-governance.md`,
  and Lending Ops' written answers of 2026-08-13.

---

## Context

An examiner asks whether credit decisions fall differently across protected groups. Meridian
cannot answer. Nothing in this platform measures outcome disparity, and the model card says so
in those words (`docs/model-card-decisioning-scorecard.md` §7, "Planned, not active").

Answering the question requires two things that sit in different places. The outcomes are here:
`decision_events` holds every approve, refer and deny with its policy band, model score and Reg B
principal reasons. Group membership is not here, and how it is obtained differs by product:

- **The dwelling-secured book.** Meridian runs a small home-equity line and collects race,
  ethnicity, sex and marital status for it under Reg B. That data lives in the origination
  system. Lending Ops' instruction is verbatim: *"it lives in the origination system — not in
  this platform, and I want it kept that way. Do not ingest it here."*
- **Everything else.** Reg B generally prohibits collecting those characteristics for
  non-dwelling-secured consumer credit, so membership is estimated with BISG — Bayesian Improved
  Surname Geocoding — from the applicant's surname and their address geocoded to block group
  (`docs/specs/fair-lending-monitoring-week8.md` §3.2).

So a join is unavoidable, and the question this ADR settles is **where it happens**. The choice
is not administrative. It decides whether a race-correlated field comes to rest inside the
platform that runs the scorecard.

That matters because of a claim already made in writing. The model card states the scorecard
reads six inputs — bureau score, annual income, requested amount, term, monthly debt, length of
employment — and that none is a prohibited basis. Lending Ops asked for that claim to be
demonstrable rather than asserted, and it is: `services/decision-service/tests/test_model_vendor.py`
demonstrates it. A claim of that kind holds for one of two reasons. Either the data is not
present, or people are careful. Only the first survives a review of the code by someone who did
not write it.

One shared database makes the distinction sharper than it would otherwise be. Under ADR 0002 all
seven services read the same schema, so "stored in this platform" means reachable from
`decision-service` by construction, whatever service writes it.

---

## Decision

**We will have the platform emit and the reporting environment compute. No protected
characteristic and no proxy probability is written back into this platform.**

### Decision 1 — The platform's contribution is an export, not a computation

Per reporting period, this platform exports the decision records — outcome, policy band, model
score, principal reasons — keyed by application identifier, plus the fields the reporting
environment needs to establish membership: enough to join for the direct path, and surname and
address for the proxy path.

It computes no disparity metric, estimates no probability, and stores no result. The reporting
environment performs the join, runs BISG where it is needed, and produces the report.

**Field contract (provisional — step 3 of the implementation plan makes this the reviewed
contract before the exporter is written; listed here so the boundary is not left to whoever
writes the exporter to infer):**

| Field | Source | Note |
|---|---|---|
| `application_id` | `applications.id` | join key |
| decision outcome | `decision_events.outcome` | |
| policy band | `decision_events.policy_band` | |
| model score | `decision_events.drivers` | score component only, not the full JSONB |
| principal reasons | `decision_events.principal_reasons` | |
| decision timestamp | `decision_events.decided_at` | anchors the reporting-period boundary |
| surname | derived from `applicants.name` | `applicants.name` stores a full name, not a surname field — the export derives it, a parsing step this ADR does not yet specify and the BISG match rate depends on |
| address for geocoding | `applicants.address` | one unstructured TEXT column, not discrete street/city/state/zip fields — geocoding to block group needs parsing this ADR does not yet specify |

**Explicitly excluded — never leaves this platform via this export:** SSN, date of birth, the
full application payload, free-text notes, and any protected-characteristic or proxy-probability
output (the last is already barred from existing here at all by Decision 2).

### Decision 2 — No protected characteristic and no proxy probability is stored here

No table in `db/init/001_schema.sql` gains a race, ethnicity, sex or marital-status column, and
none gains a BISG output. This is the operative constraint, and it is what makes the model card's
six-input claim rest on absence rather than on discipline.

`decision_events.inputs` stays identifier-free under ADR 0007 rule 1. The concern raised when
monitoring was first scoped — that measuring disparity would force identifiers back into decision
records — does not arise, because the linkage happens outside.

### Decision 3 — The export is a distinct egress path and is treated as one

The export carries surname and address off the platform. That is PII leaving a boundary it does
not leave today, and it is a new surface created by this decision rather than an existing one
inherited. It is authenticated, minimized to the fields named in Decision 1, and logged as an
event with a record count — not a silent batch. It runs outside the applicant-facing request
path.

---

## Options considered

### Option A — Platform emits, reporting environment computes and joins (**chosen**)

The protected characteristics never enter this platform, the proxy is never computed here, and
the identifier-free decision record survives unchanged. The cost is that the control lives where
this repository cannot test it (see Consequences).

### Option B — Ingest the collected demographic data here and join locally — **rejected**

Rejected first because the client refused it in writing, and that alone settles it. It is worth
recording the second reason, because a future reader may see the refusal soften: ingesting
special-category data would place it in the shared schema of ADR 0002, reachable by all seven
services, at a platform whose PII handling is a per-service redactor copy (ADR 0006) and whose
money paths still run on raw psycopg2. The controls that data needs do not exist here, and
building them is a larger programme than the monitoring it would serve.

### Option C — Compute and store the BISG proxy in this platform — **rejected**

This is the option that looks reasonable and is not. It appears to avoid Option B's problem: a
probability is not a collected characteristic, and it is derived from surname and address the
platform already holds. But a per-applicant probability of group membership is a race-correlated
field, and storing it puts one within reach of the decisioning path. The model card's six-input
claim would then hold only because nobody has joined that table to the scorecard yet — an
assertion about behavior, not about data. It also splits BISG across two environments, since the
geocoding source lives with the reporting environment (Open Item 4), leaving the platform
computing half a method.

### Option D — Re-introduce identifiers into decision records so the join can happen here — **rejected**

This is the trade-off the original monitoring ask explicitly warned about. It reverses ADR 0007
rule 1 and ADR 0008's identifier-free record for the benefit of a join that Option A performs
without any change to the record at all. Rejected as unnecessary rather than merely undesirable:
there is no capability it buys that Option A does not already have.

---

## Consequences

### Positive

- The model card's six-input claim rests on the data not being here, and stays demonstrable by a
  test that already exists.
- `decision_events` keeps the identifier-free shape ADR 0007 rule 1 and ADR 0008 depend on, so
  the assistant (ADR 0009) and the retention design are unaffected.
- The client's written instruction is honored exactly, with no interpretation applied to it.
- The platform's obligation is small and testable: produce a defined set of fields. That is the
  half we can gate.

### Negative / trade-off (accepted)

- **The control lives where this repository cannot verify it.** No CI gate here can show the
  report was produced, was correct, or was read. This platform can be gated only on its export.
  We accept a control we contribute to and do not own.
- **The export is a new PII egress path.** Surname and address leave the boundary. Decision 3
  constrains it; it does not remove it. This is a real increase in surface, and stating it is
  part of the decision rather than a caveat on it.
- **Correctness now depends on a system that has no name.** Open Item 1 of the spec asks Lending
  Ops to name the reporting environment and its operator. Until answered, this ADR describes a
  boundary whose far side is unspecified.
- **BISG's error characteristics cannot be measured here.** Accuracy is uneven across groups
  (`docs/specs/fair-lending-monitoring-week8.md` §3.2). The platform cannot calibrate what it does
  not compute, so those characteristics must be recorded alongside each report by whoever runs it.

### Neutral

- No migration. This decision is enforced by what is absent from the schema, so the DDL does not
  change and `db/migrations/` gains nothing.
- Coverage is bounded by the spec's own Known Gaps, not by this decision: notice delivery is
  unprovable (Gap 1, carded as G1), pricing disparity is unmeasurable because pricing does not
  vary (Gap 2, `POLICY_RATE_PCT = 7.99`), and referred applications have no recorded disposition
  (Gap 3). Moving the computation would fix none of them.

---

## Cross-cutting concerns

**Security.** The export is the only new attack surface, and it carries PII. It requires an
authenticated caller, the field list of Decision 1 and no more, and an audit line per run. It
must never be reachable from the borrower-facing gateway; the shared-secret pattern that
`kyc-service`, `decision-service` and `disclosure-service` already use is the fit, and it fails
closed when unconfigured.

**Performance and scalability.** A periodic batch outside the request path. It reads
`decision_events`, which is append-only and indexed by `app_id`. Volume is unknown to us (Open
Item 2), so cadence is proposed rather than calibrated.

**Reliability.** A missed export is a missed reporting period, not a customer-visible failure.
It fails closed and loudly: an export that cannot read its source aborts rather than emitting a
short file, the same posture as the reconciliation job in ADR 0015 and the fee-schedule loader.

**Maintainability.** One export path, no new abstraction, and no per-service copy — the
duplication `redactor-drift` exists to police is the pattern to avoid here.

**Cost and operational impact.** Near zero here. The cost lands on whoever operates the
reporting environment, which is precisely why Open Item 1 and Open Item 3 must be answered before
the report is relied on.

**Testing impact.** What this repository can prove: that no protected-characteristic or proxy
column exists in the schema, that the export emits exactly the agreed field list, and that the
scorecard reads six inputs. What it cannot prove: that the report is correct. The first three
belong in CI; the last belongs to the reporting environment's own controls.

---

## Implementation plan

Nothing here is built. In order:

1. Map this ADR in `scripts/spec_gate_map.txt` alongside the week-8 spec, so `spec-diff-gate`
   holds it against `services/decision-service/app`.
2. Add a schema assertion test: no table carries a race, ethnicity, sex, marital-status or
   proxy-probability column. This is the enforcement of Decision 2, and it is the one part of
   this ADR that a test can hold permanently.
3. Define the export's field list as a reviewed contract before writing the exporter.
4. Build the export behind the internal-service secret, with the audit line of Decision 3.
5. Answer Open Items 1 and 4 with Lending Ops before any report produced from the export is
   relied on.

Steps 1 and 2 are small and can land before the export exists — they constrain the platform,
which is the part this ADR is actually about.

---

## Rollback strategy

Cheap now, expensive later, which argues for settling it in this cycle.

Nothing is built, so reversing to Option B or C today costs a migration and the controls that
data would need. Once the export exists, reversal means the migration, the controls, **and**
retracting the model card's six-input claim in the form Lending Ops asked for it — that sentence
is the one an examiner reads first, and it cannot be quietly weakened.

The step that is genuinely hard to undo is not the export. It is the first column that stores a
protected characteristic or a proxy probability, because from that point the claim rests on
discipline and every later reader must audit behavior instead of schema.

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| The reporting environment is never named, and the report is never produced — leaving a merged spec and ADR describing a control that does not run | Open Item 1 is with Lending Ops; the model card already says monitoring is planned and not active, so the platform makes no claim it cannot back |
| The export becomes a de facto data feed, growing fields until it carries what Option B was rejected for | The field list is a reviewed contract (step 3), and the schema assertion (step 2) blocks the write-back direction independently of what the export carries |
| Someone adds a proxy or characteristic column later without reading this ADR | The schema assertion test fails, and `spec-diff-gate` keeps this ADR bound to `decision-service` |
| Surname and address in the export are treated as less sensitive than the SSN the redactor targets | Decision 3 names them as PII explicitly; the export is authenticated and logged, not a file drop |
| The proxy's uneven accuracy is read as a measurement rather than an estimate | The spec states the error characteristics, and Lending Ops asked for them stated rather than glossed; they are recorded with each report |

---

## Assumptions challenged

- **"A probability is not a protected characteristic, so storing it is a smaller step."** It is
  not smaller in the way that matters. The reason to keep the characteristic out is that a
  race-correlated field near the scorecard makes the six-input claim unverifiable, and a
  per-applicant probability of group membership is exactly such a field.
- **"The client's refusal is the whole reason, so the ADR is a formality."** The refusal settles
  Option B and nothing else. Option C does not ingest the client's data at all, and would have
  been available even under the refusal — it is rejected on the platform's own grounds.
- **"The decision is already recorded in the spec."** The spec states the decision and two
  reasons for it. It does not record what was rejected or why, and an option's rejection reason
  is the part a future reader needs when the option is proposed again.
- **"Monitoring forces identifiers back into decision records."** This was assumed when the ask
  was first scoped. It is false under Option A, and the assumption is retired here.

---

## Sign-off status

**Proposed.** The decision is settled with Lending Ops in writing and the spec has merged; this
ADR records the reasoning and the rejected options behind it. Open Items 1–4 of
`docs/specs/fair-lending-monitoring-week8.md` remain with the client, and Open Items 1 and 4 gate
reliance on any report, not this decision.
