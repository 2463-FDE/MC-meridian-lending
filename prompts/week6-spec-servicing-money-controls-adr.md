# Spec: Week 6 servicing money-controls ADR

Self-contained. Run Explore → Plan → Implement → Commit + PR. PR is the unit of delivery.

## Context

Client ask (Dana, VP Lending Ops): servicing dashboard so reps can adjust balances / waive fees.
This PR does not build the dashboard and does not ship any code/schema change — it locks the
design decisions a future build depends on. A companion PR (`docs/servicing-comprehension-week6`,
may or may not be merged yet — read it if present, otherwise re-derive from the ground truth
below) delivers the comprehension report and characterization/lost-update tests this ADR cites as
evidence.

Branch from `main`: `docs/servicing-money-controls-adr-week6`.

**ADR number: check `adr/` for the current highest number before writing — do not hardcode 0013,
it may already be taken by unrelated work by the time this spec runs. Use highest+1.**

## Business problem (state this before any technical solution, per ADR format rules)

Servicing money endpoints (`adjust-balance`, `waive-fee`, `apply-payment`, `late-fee`) have no
access control and no ledger:
- Any authenticated caller, any role, any `loan_id` can move money or waive a fee — zero
  approvers, zero segregation of duties (debt **D8**, Critical).
- Concurrent same-column writes (two `apply_payment`, or `apply_payment` + `adjust_balance`) lose
  an update — non-atomic read-modify-write, no lock, no version column.
- Balance history is unreconstructable — `balances` is one mutable column, no actor/delta/reason
  is ever recorded (debt **D2**, **D20**).

Ground truth to cite as evidence (verify each still holds before citing):
- `services/servicing-service/app/main.py:103,114` — `x_user_role` declared, never read, on
  `adjust_balance`/`waive_fee`.
- `services/servicing-service/app/balance.py:23-32` — read-modify-write, two autocommit round
  trips (`db.py:13`), no `FOR UPDATE`, no version.
- `db/init/001_schema.sql:117-122` — `balances.balance` is `DOUBLE PRECISION`, single mutable
  column.
- `db/init/001_schema.sql:148-179` — `decision_events`, the one true append-only table in this
  schema (SERIAL PK, JSONB payload, trigger-enforced immutability) — the pattern to mirror.
- `services/gateway/app/main.py:171` — gateway forwards authoritative `X-User-Role`/`X-User-Id`;
  servicing reads neither.
- `services/origination-service/app/authz.py` — existing RBAC shape (`require_officer`,
  `_OFFICER_ROLES = {"underwriter","admin"}`, 403) to reuse rather than inventing a new pattern.
- `adr/0010-application-ownership-authorization.md` — prior RBAC seed; its consequences section
  states a follow-on ADR would generalize role checks. This is that ADR.
- `adr/0012-decimal-minor-units-and-externalized-rule-config.md` — precedent for the money-type
  decision (Decimal/minor-units vs float) this ADR must also decide for servicing.
- `docs/debt-log.md` — quote D8, D2, D20 verbatim, don't paraphrase from memory.

## Decisions this ADR must make (present 3+ options each, per the global ADR rules)

1. **Authorization model.** Options to compare: (a) reuse `origination-service/app/authz.py`
   shape directly in servicing, (b) gateway-side role enforcement only, (c) do nothing / accept
   risk. Recommend and defend one — locked steer is (a), RBAC reuse, but justify it in the ADR
   rather than asserting it.
2. **Maker-checker breadth.** Options: (a) manual discretionary moves only — `adjust-balance` +
   `waive-fee` (`apply-payment`/`late-fee` stay automated, system/rule-driven, not discretionary),
   (b) all money-affecting endpoints, (c) threshold-based (dollar amount triggers second
   approval). Locked steer is (a) — defend it against (b) and (c), including why over-scoping to
   automated paths would be gold-plating per this repo's YAGNI rule.
3. **Ledger vs mutable column.** Options: (a) append-only ledger mirroring `decision_events`
   (SERIAL PK, JSONB/typed payload, `BEFORE UPDATE OR DELETE` trigger raising exception, no-
   truncate trigger, UNIQUE idempotency index; `balance = SUM(postings)` as a projection), (b)
   keep the mutable column, add an audit trigger, (c) event-source fully (drop the projection,
   recompute on read every time). Recommend (a); contrast explicitly against **D20**
   (`audit_logs`) as the anti-pattern this must avoid repeating (mutable, soft-deletable, so not
   trustworthy as a ledger even if it were populated).
4. **Money representation.** Options: (a) Decimal/minor-units per ADR 0012's precedent, (b) keep
   float and accept D2, (c) minor-units only for the new ledger table, leave `balances` as float
   (mixed). Recommend one and state the migration cost/blast-radius trade-off — this is a decision
   about the *target*, no migration ships this week.

## Deliverable (this PR only)

`adr/NNNN-servicing-money-controls.md` (NNNN = highest existing ADR number + 1) — Nygard format
per the global ADR rules (`~/.claude/CLAUDE.md`): Context (present tense) → Decision ("We will...")
→ Consequences (present/future, no tense drift). Business problem before technical solution. 3+
options per decision point above, each with why it's rejected. Trade-offs, rollback strategy,
risks with mitigations. Cover security, performance, scalability, reliability, maintainability,
cost, operational impact, testing impact. Use only established project ADR vocabulary — LOS, LSS,
ledger, maker-checker, append-only, fail closed, D-numbers (e.g. D8) — no invented metaphors
(check the vocabulary list in this repo's `CLAUDE.md` before writing; if a needed term isn't on
that list, use the plain concrete phrase instead of coining a label).

Reference the companion comprehension-report PR's findings as evidence where relevant (Q1-Q4), but
this ADR stands on its own — re-verify the cited file:line facts against current code rather than
trusting this spec's copy of them, in case the code moved since this spec was written.

## Explicitly out of scope (say so in the ADR's consequences/rollback section)

No schema/migration ships this week — the ADR only proposes the ledger DDL shape narratively or
as illustrative SQL in the doc, not as a runnable migration file. No endpoint/authz code change.
No `balance.py` fix. Those are follow-on PRs the ADR's "implementation plan" section should
outline as future work, not build now.

## Verification before opening the PR

- ADR renders as valid markdown; heading structure matches Nygard format.
- `adr/` numbering stays contiguous (no gap, no collision with a number taken by other work
  landed on `main` since this spec was written — re-check before naming the file).
- No file under `db/`, `services/`, `frontend/`, or `scripts/` is touched — this PR is docs-only,
  so `migration-numbering-gate` and every code-path CI gate are unaffected by construction.
- If the ADR file gets backticked repo-paths, run `./scripts/check_doc_paths.sh` to satisfy
  `doc-path-lint`.
- Diff stays under ~400 changed lines (W7-10 size rule) — one new file, no code.
