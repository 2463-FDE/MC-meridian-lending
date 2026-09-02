# ADR 0023: Applicant SSN at Rest

- **Status:** **Proposed** — partly built. Decision 1 (reduce internal exposure, independent
  of retention) is implemented, covered by tests, merged to `main` as
  [PR #142](https://github.com/2463-FDE/MC-meridian-lending/pull/142) (`25157e4`), and held by
  the blocking `ssn-purge-gate` (the last-4 hop at intake, and the purge scaffold's refusal)
  and the blocking `db-readiness-gate` (the `ssn_last4` rung). Decision 2 (the
  retention-contingent remediation) is not built anywhere; it is blocked on a client answer.
- **Reading the "Built" markers below:** **Built** in this ADR means *merged to `main`* —
  `db/migrations/0023_applicants_ssn_last4.sql`, `services/origination-service/app/purge_ssn.py`
  and the `ssn-purge-gate` job all ship there as of PR #142 (`25157e4`).
- **Date:** 2026-08-31
- **Author:** Claude Code
- **Related:** ADR 0002 (single shared database — why every service reads `applicants.ssn`
  under the same credential), ADR 0006 (logging redaction), ADR 0013 Decision 2 (the CVV/PAN
  purge — precedent for technique, explicitly **not** for shape; see *Assumptions challenged*).
  Debt D33 (this entry), D35 (SSN in transit, deferred separately), D5 (the pre-redaction
  log-file residual — note D5 does *not* cover database backups or WAL; see implementation
  plan step 5), D13b (PAN tokenization, same purge-vs-encrypt fork).
- **Source:** `docs/debt-log.md` D33, `docs/handoffs/2026-08-31-docs-glba-encryption-framing.md`.

---

## Context

`applicants.ssn` is a plaintext `TEXT` column (`db/init/001_schema.sql`). The only place in
the repository that records this fact is a trailing comment on the declaration itself. There
is one shared Postgres database and one schema (ADR 0002), so all seven services read the
column under the same credential; nothing scopes access to the two that need it
(`kyc-service`, `decision-service`).

This is not a discretionary hardening question. The **GLBA Safeguards Rule, 16 CFR Part 314**
(FTC, applies to non-bank financial institutions) was amended in 2021 to add **§314.4(c)(3)**:
encrypt customer information *"in transit over external networks and at rest."* The two halves
of that sentence do not carry the same status here. The in-transit half is qualified to
*external* networks — every hop in this stack sits on one Docker bridge network on one host, so
that half is deferred separately (D35) and the deferral is correct today. The at-rest half
carries no such qualifier. A plaintext `applicants.ssn` sits inside the requirement regardless
of network topology, and the rule's only recognized exception is a compensating control
approved **in writing** by the institution's designated Qualified Individual. Nobody has done
that. Depending on where the client lends, NY DFS Part 500 (23 NYCRR 500.15) may add a second,
overlapping encryption-at-rest requirement. State breach-notification statutes near-universally
treat an SSN as a notification trigger and most provide a safe harbor for encrypted data, so
this control also decides whether a future incident is reportable. Every citation in this
paragraph is engineering's reading of the rules, not counsel's — *Regulatory basis and review
status* below separates what was cited from what is assumed, and names who confirms it.

Nothing prohibits *storing* a full SSN — bureau furnishing under FCRA, TIN reporting, and the
CIP flow this platform implements all need it at some point. The question this ADR answers is
narrower and non-optional: how the value is protected while it sits in the column, and for how
long it needs to sit there at all. Whether the applicable law requires action is not a call
this register makes; that a rule is on point, so the remediation is not discretionary, is the
fact being recorded.

The two services that read the column need different amounts of it. `decision-service`
(`app/decision.py:330`, `_pull_credit`) needs the real digits — it is the sole consumer of the
value for its stated purpose. `kyc-service` (`app/kyc.py:28`) does not: its check is
`bool(applicant.get("ssn"))`, a presence check, not a verification against the value. That gap
is what makes a partial remediation available immediately, without waiting on the retention
answer below.

### Regulatory basis and review status

**None of the legal reading in this ADR has been reviewed by counsel or by the client's
Compliance Officer.** It is an engineering assumption about which rules are on point, written
so the question can be put to someone qualified to answer it — not a legal conclusion. Who
confirms it: Priya (Compliance Officer) for the client's own regulatory posture, and the
client's outside counsel for anything turning on scope or interpretation. Until one of them
signs off, treat the obligation framing below as *the reason to ask*, not as the answer.

| Claim in this ADR | Citation | Status |
|---|---|---|
| Encryption of customer information at rest is required, with no network-topology qualifier | GLBA Safeguards Rule, 16 CFR Part 314 § 314.4(c)(3) (added by the 2021 amendments) | Cited; text read directly. **Whether this client is a "financial institution" under Part 314, and whether an applicant SSN is "customer information" pre-decision, are both unreviewed.** |
| The only exception is a compensating control approved in writing by the Qualified Individual | 16 CFR Part 314 § 314.4(c)(3) | Cited. Whether an equivalent control could be argued here is unreviewed. |
| A second, overlapping at-rest requirement may apply depending on where the client lends | 23 NYCRR 500.15 (NY DFS Part 500) | Cited. **Applicability is unknown** — nobody has established where the client is licensed. Engineering assumption. |
| Record retention bounds the window if retention is required | ECOA / Reg B, 12 CFR 1002.12 (25 months) | Cited. **Whether the *raw* SSN is in scope of that rule is counsel's read, not engineering's** — stated as unresolved throughout this ADR. |
| Nothing prohibits storing the SSN; FCRA furnishing, TIN reporting and CIP need it | FCRA (15 U.S.C. § 1681 et seq.); IRS TIN reporting; the CIP flow this platform implements | Engineering assumption from the platform's own data flow. Not counsel-reviewed. |
| State breach-notification statutes treat an SSN as a trigger and commonly provide an encryption safe harbor | State statutes, not enumerated here | **Engineering assumption.** No specific state statute was read; the generalization is offered as a reason the control matters, not as a finding. |

## Decision

### Decision 1 — Reduce internal exposure now, independent of the retention answer (Built)

We will add `applicants.ssn_last4` (migration `db/migrations/0023_applicants_ssn_last4.sql`,
same number as this ADR by coincidence of two independent sequences — migrations and ADRs do
not share a numbering space), populated at intake and backfilled on existing rows. Both call
sites that previously sent `kyc-service` the full SSN (`applications.py` submit and
`recheck-kyc`) now send only the last four characters. `kyc-service`'s own check is
`bool(value)`; a non-empty last-4 satisfies it identically to a non-empty full value, so this
is not a behaviour change. The blocking `ssn-purge-gate` runs `test_intake.py`, which pins
that intake writes `ssn_last4` on every submit and that the KYC hop carries last-4 — those
assertions also run in the `backend` matrix, which is `continue-on-error` + `|| true`, so
without a blocking job a regression would put full SSNs back on the wire on a green build.
The origination readiness rung (`app/config.py`, held by the blocking `db-readiness-gate`)
asserts the column exists and is `text`, so a volume that predates the migration reports
unhealthy rather than 500ing intake or recheck on a missing column.
`decision-service`'s bureau pull is untouched — it still reads the full column, because it
must.

#### Options considered

| Option | Why rejected |
|---|---|
| **A. Chosen: `ssn_last4` column, sent to kyc-service instead of the full value** | Cuts the number of services that see the full SSN on the wire from two to one, today, with no dependency on the retention answer. |
| **B. A boolean presence flag instead of last-4** | Seriously considered — `kyc-service`'s actual need is exactly `bool(value)`, and a flag is strictly less PII for identical functionality. Rejected for this pass on a usability ground: last-4 is the form a CSR verifying a caller over the phone actually uses, and building the boolean version now forecloses that without a product answer on whether CSR verification needs it. Recorded as a live open question, not a settled no — see *Assumptions challenged*. |
| **C. Status quo — kyc-service continues receiving the full value** | Rejected. It is strictly worse than Option A on the same axis Option A improves, for no offsetting benefit; `kyc-service` never uses the extra digits. |

### Decision 2 — The retention-contingent remediation: purge preferred, encryption as fallback (Proposed, not built)

We will treat **purge to `ssn_last4` at application-terminal-state** as the preferred
remediation, with **application-level encryption** as the fallback if the client's retention
answer forecloses purging. Both remain live until that answer arrives; neither is built.

The fork depends on one question, put to Priya (Compliance Officer) and Dana (VP Lending
Ops): **how long after submission must the platform be able to re-run a bureau pull?** *Not
once the decision is final* selects purge — no key management, the risk is removed rather
than relocated. *Any time within the retention window* selects encryption — the column must
persist, which requires a key-management answer (rotation, re-encryption, who holds the key)
that itself waits on a deployment existing (the same blocker as D35). The record-retention
rule (ECOA/Reg B, 25 months, 12 CFR 1002.12) bounds the window if retention is required, but
whether the *raw* SSN is in scope under that rule is counsel's read, not engineering's.

A scaffold for the purge path exists (`services/origination-service/app/purge_ssn.py`) but its
eligibility query is a **known-wrong placeholder** and is documented as such in its own
docstring: it selects on calendar age since submission. The correct trigger is the
application reaching a terminal state (decided/funded/declined) for every `applications` row
tied to that applicant — recurring and event-driven, not a one-shot migration (see
*Assumptions challenged*). The scaffold ships three independent safety gates
(`SSN_PURGE_ENABLED` env flag, an explicit `--execute`, and an in-code
`_ELIGIBILITY_IS_PLACEHOLDER` constant that makes `run()` raise rather than purge while it
stands) and the CLI's dry-run/reporting shape, which the corrected query can reuse. The third
gate exists because the first two are both operator-flippable — an env var and a CLI flag —
and the eligibility query below is known-wrong, so a code-level refusal is what stands between
that query and a live run, not just a docstring. Clearing it is a reviewed code change made in
the same diff that replaces the calendar-age `WHERE` clause below with the
applications-terminal-state join.

#### Options considered

| Option | Why rejected |
|---|---|
| **A. Chosen (preferred): purge to `ssn_last4` at terminal state** | The only option that removes the risk rather than relocating it, and the only one that needs no key-management answer. Selected if the retention answer permits it. |
| **B. Chosen (fallback): application-level encryption, key from a secret manager** | Correct if the retention answer requires the value to persist. Needs a key-management story (rotation, re-encryption, both read sites changed) and a deployment to hold the key, so it is a second build, not an increment on Decision 1 — not a quick fallback in practice, only in framing. |
| **C. `pgcrypto` column encryption** | Rejected outright, independent of the retention answer. The key is reachable by the same database credential that is the threat this ADR responds to (ADR 0002: one shared schema, one credential, all seven services). It protects against a stolen disk and nothing in the actual threat set — the same critique this repository already made of ADR 0003's disk-encryption argument for card data. |

## Consequences

### Positive

- The number of services that see the full SSN on the wire drops from two to one as soon as
  Decision 1 merges, with no dependency on any external answer. Until it does, it is still
  two: `main` sends `kyc-service` the full value.
- Whichever path Decision 2 resolves to, the fork is already named with its trade-offs
  recorded, so the client's answer selects a path rather than triggering a design exercise.
- The purge scaffold's safety shape (three independent gates, dry-run default, reporting
  format) is reusable once the eligibility query is corrected — it does not need to be
  built twice, only the `WHERE` clause does.

### Negative / tradeoff (accepted)

- **`applicants.ssn` stays plaintext until Decision 2 resolves.** Decision 1 does not close
  D33; it narrows what Decision 2 has to fix.
- **`ssn_last4` is itself PII beside a name and DOB**, more than the functionality it serves
  strictly requires (Decision 1, Option B). Accepted for now as a considered trade, not an
  oversight.
- **The purge scaffold exists but cannot be enabled.** Shipping an inert, documented-wrong
  placeholder is a deliberate choice — it reuses safety gates and shape later — but it is
  still unfinished work.
- **Encryption, if selected, is a second build that waits on a deployment existing.** The
  same dependency already blocks D35; this ADR does not remove it.

### Neutral

- `applicants` gains one nullable column (`ssn_last4`) regardless of which Decision 2 path
  is eventually selected.

## Cross-cutting concerns

**Security.** Decision 1 shrinks the set of services holding the full value without waiting
on anything. Decision 2's two live paths both reduce exposure further; `pgcrypto` is rejected
specifically because it would not, given the threat model ADR 0002 already establishes (one
shared credential). Redaction and logging paths are unaffected — neither `ssn` nor
`ssn_last4` is logged anywhere in this change.

**Performance.** Decision 1 adds one column and one slice operation per intake/recheck call;
immaterial. Decision 2's purge path, once corrected, updates one row per terminal-state
transition rather than in bulk, so it does not introduce a batch cost; the open question is
whether that per-row `UPDATE`'s dead tuples need scheduled maintenance (see *Assumptions
challenged*), which is a maintenance-window cost, not a request-latency one.

**Scalability.** No new service, no new datastore. Both Decision 2 paths operate within the
existing shared database.

**Reliability.** The readiness rung added for `ssn_last4` (Decision 1) makes an unmigrated
volume fail closed at `/health` rather than 500ing intake or recheck-kyc, the same pattern
every other schema-dependent rung in this codebase follows.

**Maintainability.** Decision 1 is a small, self-contained change. Decision 2 deliberately
defers its harder engineering (event-driven purge triggering, or key management) rather than
building either speculatively before the client answer is known — building the wrong shape
now would cost more to unwind than building nothing.

**Cost.** Encryption's cost is mostly deferred key-management infrastructure, not measured
here. Purge has no comparable infrastructure cost, which is part of why it is preferred when
available.

**Operational impact.** The purge path, once corrected, needs an operator procedure for
whatever reclaims dead tuples on a schedule that does not lock `applicants` the way a
`VACUUM FULL` would (see *Assumptions challenged*) — not yet written, blocked on that
question. Encryption's operational cost is key rotation and incident-response key recovery,
neither designed yet.

**Testing impact.** Decision 1 is covered, and every one of its tests sits under a blocking
job rather than the tolerated `backend` matrix, held on `main` since PR #142 (`25157e4`):
`ssn-purge-gate` runs `test_intake.py` (intake
writes `ssn_last4`; the KYC hop carries last-4, not the full value) and `test_purge_ssn.py`;
`db-readiness-gate` runs `test_db_readiness.py` (the rung fires on an unmigrated volume);
`adr-0010-authz-gate` runs `test_authz.py` and `kyc-enforcement-gate` runs `test_kyc_gate.py`,
both of which assert the presence-check behaviour is unchanged. Decision 2 has no tests beyond
the purge scaffold's own gate-and-dry-run unit tests — its eligibility logic is untested
because it is known-wrong and unfinished by design.

## Implementation plan

1. `ssn_last4` column — migration `db/migrations/0023_applicants_ssn_last4.sql`, backfilled.
   **Built.**
2. Submit and recheck-kyc send `kyc-service` last-4 instead of the full value
   (`app/intake.py::ssn_last4`), held by the blocking `ssn-purge-gate`. **Built.**
3. Readiness rung for `applicants.ssn_last4` in `app/config.py`, covered by the existing
   blocking `db-readiness-gate`. **Built.**
4. Purge CLI scaffold (`app/purge_ssn.py`) — safety gates and reporting shape only, eligibility
   query is a documented placeholder; the refusal is held by the blocking `ssn-purge-gate`.
   **Built, not enabled.**
5. **Where each residual a purge cannot reach is tracked.** Sequenced here, ahead of the
   remediation, because this ADR already relies on these residuals to argue that nulling the
   column does not close D33 — if they are only named after the remediation lands, that
   argument has no destination while it is being made. Destinations as of this ADR:
   - **Pre-redaction log files** — D5's existing open residual, "flagged here but not
     audited". No new entry; D5 owns it, and D5 is Mitigated-not-Fixed precisely because rows
     like this one are still open.
   - **Database backups and WAL segments** — **no owner before this ADR.** D5's backup
     residual reads on its own terms as backups *of the log files*, not of the database, so
     citing D5 here would claim coverage D5 does not have. Carded instead as an Open subtask
     on D33 in `docs/debt-log.md`. Migration `0020_payments_drop_cvv.sql` already records the
     same residual for cardholder data, so this is the second entry against it, not the first
     sighting.
   - **Replicas** — **explicit non-goal for this ADR.** No replica exists in any committed
     deployment (`docker-compose.yml` and `docker-compose.demo.yml` run a single `postgres`
     with no standby and no `wal_level` set for replication). It joins the backup/WAL subtask
     the day one exists; that trigger is recorded here so it is not rediscovered.
   - **The `ssn_last4` column itself** — deliberately out of scope for any purge: it is the
     value the purge preserves (Decision 1, Option B records the live question about whether
     it should exist at all).

   **Not started as a retention action** — this step tracks where each residual lives, which
   is done; taking the action for each is owned by D5 or by the trigger above, not by this ADR.
6. Get the retention-window answer from Priya/Dana. **Not started — the actual blocker.**
7. Rework the purge eligibility query to a terminal-state, per-applicant trigger; resolve the
   autovacuum-vs-scheduled-maintenance question named in *Assumptions challenged*; write the
   real migration. **Not started, contingent on step 6.**
8. If the answer instead requires retention, scope the encryption build (key source, rotation,
   both read sites) as its own change. **Not started, contingent on step 6.**
9. **Tighten the `spec-diff-gate` mapping for this ADR.** `scripts/spec_gate_map.txt` mapped ADR
   0023 to `services/origination-service/app/intake.py` alone, not to
   `services/origination-service/app/purge_ssn.py` — the file whose eligibility rule, keep-last-4
   behaviour and inertness Decision 2 constrains — because `purge_ssn.py` did not exist on
   `main` yet and `spec-diff-gate` is an existence check. **Done**, now that `purge_ssn.py` is on
   `main` (PR #142, `25157e4`): `purge_ssn.py` is mapped in the same commit as this doc fix.
   `intake.py` keeps its line rather than being replaced — step 2's last-4 hop is a built
   obligation of this ADR, and `purge_ssn.py` is an inert scaffold that may yet be retired, so a
   single line on it would let a future deletion leave this ADR unmapped and invite a false
   `# EXEMPT` while intake's obligation still ships. A regression assertion in
   `scripts/test_spec_diff_gate.sh` (blocking job `spec-diff-gate-tests`) pins both lines.

## Rollback strategy

Decision 1 is additive and fully reversible: drop `ssn_last4`, revert the two call sites to
sending the full value, remove the readiness rung. Nothing downstream depends on the column
existing.

The purge scaffold (`app/purge_ssn.py`) can be deleted with no effect on running behaviour —
it has never executed a purge, by construction.

**Once Decision 2's purge path actually runs, it is not reversible.** Nulling `applicants.ssn`
destroys the value; that is the point. This is the same posture ADR 0013 takes for its CVV/PAN
purge: the rollback for a defect discovered after purging is to fix forward, not to restore
data that no longer exists. If Decision 2 instead resolves to encryption, that path is
reversible in principle (decrypt back) but requires the key to still be held — losing the key
is equivalent to a purge in effect, which is itself a risk the key-management design has to
carry, not a rollback strategy this ADR can specify in advance.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| The purge scaffold is enabled before its eligibility query is corrected, purging the SSN of an applicant still awaiting a decision | Three gates, not two: `SSN_PURGE_ENABLED` and `--execute` are both required and neither is set, and even with both set `run()` raises `PurgeAbort` (exit 2, not a clean 0) while the in-code `_ELIGIBILITY_IS_PLACEHOLDER` constant stands — an env var and a CLI flag are both operator-flippable with no review, so the refusal that actually matters is the one that needs a reviewed diff to lift. The blocking `ssn-purge-gate` is what keeps that constant load-bearing rather than decorative: it proves `--execute` with the env gate open still refuses, never opens a transaction, and exits 2 rather than a clean 0. |
| Decision 2 stalls indefinitely on the client answer, leaving `applicants.ssn` plaintext with no forcing function | D33 is carded High in `docs/debt-log.md` — note that entry ranks it *below* D21b on exploitability and explicitly says it is "not the thing to do first", so priority alone is not the forcing function. What forces it is the obligation recorded under *Sign-off status*, and this ADR names the exact question blocking it so it is not rediscovered by a future session. |
| The eventual purge migration ships without solving the dead-tuple reclaim question, so "purged" means only "nulled in the live row" | Named explicitly in *Assumptions challenged* and in the Implementation plan as a precondition of step 6, not an afterthought to discover during that migration's review. |
| Encryption is selected and ships with a placeholder or hardcoded key, the same class of defect this rule exists to prevent | `decision-service`'s `_ssn_fingerprint` pepper pattern (`app/config.py`) is the precedent to mirror: refuse a known-placeholder value, report unhealthy outside development without a real one. Cite this ADR's Decision 2 when that build starts. |
| `ssn_last4` is later judged to have been the wrong call (Decision 1, Option B) after CSR-verification requirements turn out not to need it | Removing a column is a strictly smaller change than adding one under a readiness rung already in place; the option was recorded as live, not closed, for exactly this reason. |

## Assumptions challenged

- **"Leave it as it is until the client answers."** False. GLBA's at-rest requirement carries
  no topology qualifier and its only exception is a written, Qualified-Individual-approved
  compensating control, which does not exist here. The retention answer selects *which*
  remediation, not *whether* one happens.
- **"The purge is the same shape as the CVV/PAN purge (ADR 0013 Decision 2, migration
  0020)."** False, and corrected in this ADR relative to an earlier debt-log draft that made
  this claim. 0020 dropped a column once, because SAD retention is prohibited outright with
  no legitimate ongoing need for the value. D33 cannot drop the column the same way: the
  bureau pull needs the real digits while an application is still decisionable, so the
  column stays and the *value* is nulled per-applicant at terminal state — recurring and
  event-driven. That also means the single `VACUUM FULL` that closed 0020 does not transfer:
  it takes an `ACCESS EXCLUSIVE` lock on `applicants`, which a per-purge rewrite cannot afford
  without blocking intake and every officer read. Whether routine autovacuum reclaims the
  resulting dead tuples on an acceptable schedule, or whether scheduled maintenance is
  required, is unresolved and named as a precondition, not assumed either way.
- **"`kyc-service` needs the real SSN, or at least a recognizable fragment of it."** False,
  measured directly: its own code is `bool(applicant.get("ssn"))`. This is what makes
  Decision 1 available without waiting on Decision 2.
- **"Last-4 costs nothing over a boolean, so there's nothing to decide."** Not accepted as
  free. Recorded as a live, considered trade (Decision 1, Option B) rather than settled by
  default, because the cheaper direction to build is the one that removes a column later, not
  the one that adds it back after regulatory or product review flags it.
- **"Nulling the column closes D33."** False on its own terms even after Decision 2 lands.
  The same residuals ADR 0013 named for cardholder data apply identically here: pre-redaction
  log files, database backups, WAL segments, and any replica are not reached by a column
  purge and need their own retention action, owned elsewhere. Closing D33 without naming this
  would claim more coverage than the code has.

## Sign-off status

**Proposed, partly built.** Decision 1 is implemented, covered by tests, merged to `main` as
PR #142 (`25157e4`), and held by the blocking `ssn-purge-gate` and `db-readiness-gate`.
Decision 2 is engineering position only — purge preferred, encryption as the named fallback —
with the retention-window question
still open with Priya and Dana. This ADR does not resolve that question; it records what each
answer selects and why, so the answer costs a path choice rather than a design exercise.

**Two separate questions, and only the first is settled.** *May the platform store a full
SSN?* Yes — nothing prohibits it, so there is no "stop storing it" obligation, and this is
where ADR 0013's Decision 2 differs: that one faced a value whose retention is prohibited
outright, and this one does not. *Must the stored value be protected at rest?* Yes, on the
reading recorded above — § 314.4(c)(3) carries no topology qualifier and its only exception is
a written, Qualified-Individual-approved compensating control that nobody has produced. The
client's retention answer therefore selects **which** remediation ships — purge or encryption —
not **whether** one ships. Decision 2 must not be closed on the ground that storing the SSN is
lawful; lawful storage is the premise of the obligation, not a defence against it.

The one thing that could change that conclusion is the review named in *Regulatory basis and
review status*: the obligation rests on engineering's reading of which rules are on point, and
Priya or counsel can narrow it. Absent that review, Decision 2 stays an open obligation, not a
discretionary hardening item.
