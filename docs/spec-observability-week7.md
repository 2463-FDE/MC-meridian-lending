# Spec: Payment Observability and Settlement Reconciliation (Week 7)

**Status:** draft · **Written:** 2026-08-12 · **Base:** `main` @ `f375ef2`
**Client ask:** observability / SRE / guardrails — "Payments feel flaky, and my finance team
grumbles about 'noise' at month-end that they just write off."
**Companion docs:** `docs/client-asks-2026-08-12-observability.md` (the asks and the evidence),
ADR 0014 (the decisions), `docs/debt-log.md` (D7).

---

## Executive Summary

Meridian's finance team closes each month by adjusting to the bank's number. They do that
because the platform gives them no way to say *why* the two sides differ: the only comparison
that exists returns two grand totals and nothing else.

Against the artifacts the client handed over — `db/settlement.csv` and the seeded `payments`
rows — the two sides do not tie out. Scoped to the three loans and seven days the settlement
file covers, the ledger reads **2,285.35** and the settlement nets **3,174.17**, a variance of
**−888.82**. That variance is not one error. It is three unrelated defects with opposite signs,
totalling **1,753.18** in absolute terms. Netting them into a single number hides roughly half
the error and all of the causation, which is exactly what "a little noise" means in practice.

This week delivers one instrumented path and one control: a correlation id carried across the
two halves of the payment flow, and a reconciliation job that matches ledger rows to settlement
rows individually, classifies every difference, and refuses to report a clean run it did not
actually perform.

It does **not** stop the defects it finds. Prevention is specified elsewhere and scheduled
elsewhere — see Out of Scope.

---

## Problem Statement

### P1. The existing "reconciliation" is not a control

`services/servicing-service/app/reconciliation.py` computes two totals. `ledger_total()`
(line 13) is `SELECT COALESCE(SUM(amount), 0) FROM payments` over the whole table.
`settlement_total()` (line 18) sums the CSV, netting refunds. `GET /reconciliation/peek`
(`services/servicing-service/app/main.py:124`) returns both, and its own source comment says
what it is:

> `# Not a real control — just exposes the two totals. They don't tie out. (debt D7)`

Three things are wrong with it beyond the missing detail:

- **The totals are not comparable.** `ledger_total()` spans the entire `payments` table,
  including roughly 600 rows seeded for loans 7000–7299 dated from 2026-05-01
  (`db/init/003_seed_bulk.sql:74-82`). The settlement file covers three loans over seven days
  in June. The subtraction is meaningless before any defect is considered.
- **It fails open.** `settlement_total()` returns `0.0` when the file is absent
  (`reconciliation.py:19-20`) — no exception, no signal. In the deployed configuration the path
  resolves (`docker-compose.yml:92` mounts `./db/settlement.csv` at `/app/data/settlement.csv`,
  and the servicing Dockerfile sets `WORKDIR /app`), so this is latent rather than active. It is
  still a check that reports a number when it verified nothing.
- **Float money.** `payments.amount` and `balances.balance` are `DOUBLE PRECISION`
  (`db/init/001_schema.sql:119,130`). Summing floats and asking whether the result ties out is
  unsound at precisely the moment the answer matters.

### P2. The payment path spans two services with nothing tying the halves together

`payment-service.charge()` writes the `payments` row and then calls servicing's
`POST /accounts/{loan_id}/apply-payment` (`payments.py:92-110`). Neither half carries a
correlation id: `request_id` appears in `decision-service` and the origination assistant, and
**nowhere** in `payment-service`. There is no OpenTelemetry, Prometheus, or statsd anywhere
under `services/`.

Two consequences:

- The charge log line reads `"POST /payments charge req=%s -> ok"` (`payments.py:69`) and is
  emitted **before** the INSERT. It reports success for work not yet done.
- A failed apply is logged as an error (`payments.py:107`) while the caller still returns
  `status: "captured"` — the comment concedes it: *"the card was already charged and the row
  written, so we still report the charge captured."* A capture that never reached a balance
  produces one ERROR line in one service's log and no counterpart anywhere.

### P3. There is no key to reconcile on

`payments` carries no processor reference and no idempotency key — the DDL says so:
`-- no idempotency_key, no unique(charge_ref)` (`db/init/001_schema.sql:133`). The settlement
file has references (`PR-100231`…) that the platform never sees, because **no processor
integration exists**: `PROCESSOR_BASE_URL` is defined in `payment-service/app/config.py:167`
and `servicing-service/app/config.py:189` and referenced by no code path.

So matching this week is necessarily inexact. That constraint is a decision, not an oversight —
see D2 and ADR 0014.

### P4. What the data actually contains

Three defect classes, established from the handed-over artifacts:

| Loan | Ledger | Settlement (net) | Variance | Cause |
|---|---|---|---|---|
| 4471 | 599.99 | 1,099.99 | −500.00 | Two settled captures (`PR-100290`, `PR-100311`) with no ledger row — captured, never credited |
| 5582 | 821.00 | 1,642.00 | −821.00 | Ledger has two rows two seconds apart (`db/init/002_seed.sql:70-71`, seed comment `-- duplicate`) against one settled capture that day |
| 6011 | 864.36 | 432.18 | +432.18 | A settlement refund (`PR-100299`) the `payments` table cannot represent — no direction column |
| **Total** | **2,285.35** | **3,174.17** | **−888.82** | net; **1,753.18** absolute |

---

## Deliverables (In Scope)

### D1. A correlation id across the payment span

**(a) Generation.** `payment-service.charge()` mints a request-scoped id when the caller does
not supply one, mirroring `decision-service`'s existing convention: an `X-Request-Id` header is
accepted on `POST /payments` and used verbatim when present, otherwise a `uuid4()` hex is
generated. The name is `request_id`, matching `decision-service/app/decision.py` and the
`Idempotency-Key → request_id` forwarding already documented at `docs/runbook.md:83`. Do not
introduce a second vocabulary (`trace_id`, `correlation_id`) for the same concept.

**(b) Propagation.** `_apply_via_servicing` (`payments.py:92`) sends the id as an
`X-Request-Id` header on its `httpx.post`. The request **body is not changed** — it stays
`{"amount": ..., "payment_id": ...}`. The body is the payments spec's territory (week 5 D3(d)
removes `amount` from it), and changing it here would collide with that work.

**(c) Both sides log it.** Every log line on the charge path and on servicing's
`apply_payment` handler carries `request_id=<id>`. Servicing reads the header, defaulting to
`"-"` when absent so a direct call is visibly uncorrelated rather than silently untraceable.

**(d) The success line moves.** `payments.py:69` currently logs `-> ok` before the INSERT. It
becomes two lines: an entry line at the start of the span, and an outcome line after the INSERT
and the apply attempt, carrying the real outcome. A log line asserting success for work not yet
performed is the log-level form of the fail-open in P1.

**(e) Log-only, deliberately.** No column, no migration. The persistent correlation between a
capture and its application is `payments.id → payment_applications.payment_id`, already
specified in week-5 D3(b) and carrying `UNIQUE (payment_id)`. Adding a second persistent
correlation key this week would duplicate that or fight it. **This is the single most important
scope boundary in this spec.**

### D2. Row-level reconciliation with an explicit matching rule

Extends `services/servicing-service/app/reconciliation.py`. Prefer editing that module over
adding a parallel one.

**(a) Comparison window.** The job takes a date range and reconciles only rows inside it, on
both sides. Without this the comparison is the P1 defect with more steps. Default window is
derived from the settlement file's own `settlement_date` range; an explicit `--from` / `--to`
overrides it.

**(b) Money is minor units.** Both sides convert to integer cents at read time and every
comparison, sum, and reported figure is an `int`. `Decimal` is used for the parse
(`Decimal(str(value))`, then scaled), never binary float. This follows ADR 0012's precedent and
is the reason the report can state equality at all. The `payments.amount` and
`balances.balance` columns are **not** converted — that is D2 ledger work and out of scope.

**(c) The matching rule, stated as a rule.** A ledger row and a settlement row match when all
three hold:

1. `loan_id` equal,
2. `amount_minor` equal,
3. `settlement_date` within ±1 day of the ledger row's `created_at` date.

Matching is one-to-one and greedy in `(loan_id, amount_minor, date)` order; a settlement row
already matched cannot match a second ledger row. Where counts differ on an otherwise identical
tuple, the surplus rows on whichever side has more are reported as unmatched on that side.
**The job never guesses which of two identical rows is the orphan**; it reports that a count
differs.

The ±1 day tolerance exists because the ledger stamps `created_at` at capture and the processor
stamps `settlement_date` at settlement, and no cut-off convention has been confirmed by the
client (open question, see Client Questions). The tolerance is a named constant, not a literal
buried in a comparison.

**(d) The tolerance defeats duplicate detection, so duplicates get their own check.** This is
not a refinement — it is the reason (e) exists, and it was found by simulating the matcher
against the sample rather than by reasoning about it.

Loan 5582 has two ledger rows two seconds apart against settled captures on 06-01, 06-02, 06-04
and 06-07. Under a ±1 day window **both ledger rows match** — the second one pairs with 06-02 —
and `MISSING_IN_SETTLEMENT` comes back empty. The matcher reports the double charge as clean.
Simulated over the sample:

```
window ±0d:  MISSING_IN_LEDGER 5 (173150)  MISSING_IN_SETTLEMENT 1 (41050)  gross 257418
window ±1d:  MISSING_IN_LEDGER 4 (132100)  MISSING_IN_SETTLEMENT 0 (    0)  gross 175318
             net −88882 under both
```

Tightening the window to ±0 is the wrong fix: it makes the duplicate visible by accident while
misclassifying every legitimately-next-day settlement as two breaks, which inflates the report
with exactly the noise this control exists to remove.

**(e) Duplicate detection, independent of matching.** The job scans the ledger side alone for
rows sharing `(loan_id, amount_minor)` within a bounded time gap, and reports each such pair as
`DUPLICATE_SUSPECT`. This runs before matching and its result does not depend on matching, so
no window tolerance can hide it. On the sample it reports one pair: loan 5582, 41050 minor,
2 seconds apart.

`DUPLICATE_SUSPECT` is a *signal*, not a variance — it is not added to any variance figure,
because the duplicate's money is already accounted for on whichever side it landed. Counting it
twice would be the netting error in a new costume.

**(f) Break classification.** Every row that does not match lands in exactly one class:

| Class | Meaning | In the sample (±1d) |
|---|---|---|
| `MISSING_IN_LEDGER` | Settled capture with no ledger row — money captured, never credited | 4 rows, 132100 minor (loans 4471, 5582) |
| `MISSING_IN_SETTLEMENT` | Ledger row with no settled capture — credited, never captured | 0 rows — see (d) |
| `REFUND_UNREPRESENTED` | Settlement row with `type=refund`; the ledger has no direction column so it cannot hold a counterpart | 1 row, 43218 minor (loan 6011) |
| `AMOUNT_MISMATCH` | Same loan and date window, differing amount | none |
| `DUPLICATE_SUSPECT` | Two ledger rows, same loan and amount, within the gap bound — signal only, not a variance | 1 pair, loan 5582, 41050 minor |

`REFUND_UNREPRESENTED` is deliberately its own class rather than a `MISSING_IN_LEDGER` variant:
it is a schema limitation, not a lost payment, and merging them would put a known-benign row
next to customer money that went missing.

**(g) Fail closed.** The job aborts — nonzero, distinct from "reconciled, breaks found" — when
it cannot perform the comparison: settlement file absent, unreadable, empty, missing a required
column, or containing a row whose `amount` or `type` does not parse. `settlement_total()`'s
`return 0.0` on a missing file (`reconciliation.py:19-20`) is removed. A verifier must never
report a result for a path it did not verify.

Exit codes, mirroring `scripts/prove_test.sh`'s convention:

```
0  reconciled, no breaks
1  reconciled, breaks found
2  ABORT — could not run the comparison
```

"Could not check" is never reported as 0, and it is never reported as 1 either — a break count
of zero from a run that read nothing is the failure this control exists to prevent.

**(h) Read-only.** The job issues `SELECT` only. It does not correct a balance, insert a
payment, or write any table. Auto-correcting a balance would be an unauthorized money movement
with no maker-checker (D8) on the same unlocked read-modify-write that is D3
(`services/servicing-service/app/balance.py:23-32`, *"Read-modify-write with no lock. Float
math."*). This is stated in the ADR as a decision, because "while we're here, just fix the
balance" is the obvious next suggestion and the answer is no.

### D3. The break report

**(a) Invocation.** A module entrypoint on servicing —
`python -m app.reconcile --from YYYY-MM-DD --to YYYY-MM-DD` — runnable inside the container and
in CI. There is no scheduler in this stack (compose runs no cron, and `db/migrations` are
hand-applied), and this spec does **not** introduce one. "Daily" is an operational convention
documented in the runbook plus whatever cron the operator already has, not a new service.
Building a scheduler to prove a control works would be the larger half of the week spent on the
smaller half of the problem.

**(b) Output.** One JSON document on stdout, and a human summary on stderr so a terminal run is
readable and a piped run is parseable. The JSON carries: the window, per-side row counts and
minor-unit totals, the three variance figures in (c), every break with its class, loan, amount
in minor units, date, and — where the settlement side is known — the `processor_ref`, and every
`DUPLICATE_SUSPECT` pair separately from the breaks.

**(c) Three figures, and they are not interchangeable.** The report states all three, adjacent,
each labelled with what it depends on:

| Figure | Sample | Depends on matching? |
|---|---|---|
| **Net variance** — ledger total minus settlement net | −88882 | No. Stable under any window |
| **Per-loan absolute variance** — Σ\|per-loan net\| | 175318 | No |
| **Gross break value** — Σ of all break amounts | 175318 at ±1d, 257418 at ±0d | **Yes** |

Reporting only the net is the reporting failure that produced "month-end is a little noisy":
−88882 against 175318 means the netting hides roughly half the error. Reporting gross break
value *without* saying it moves with the tolerance would be a second version of the same
mistake — a figure that looks like a measurement but is partly an artifact of a constant we
chose. The two coincide at ±1 day on this sample; that is a coincidence of the data, not a
property, and the report must not let a reader infer otherwise.

**(d) `peek` stops lying.** `GET /reconciliation/peek` (`main.py:124`) is rewritten to return
the break summary for the default window from the same code path as (b), and its
"not a real control" comment is deleted because it is no longer true. Keeping a second,
weaker comparison alive next to the real one reproduces the drift the fee-schedule loader was
built to end. `ledger_total()` and `settlement_total()` are removed rather than left beside
their replacement.

### D4. One alert

A single alert on the reconciliation outcome: **per-loan absolute variance in the window exceeds
a threshold**, expressed in minor units, sourced from configuration with no default (fail
closed, mirroring `disclosure-service/app/rules.py`).

Three properties, each chosen against a rejected alternative:

- **Absolute, not net.** Netting is the failure mode — it is what let three defects present as
  one small number.
- **Per-loan absolute variance, not gross break value.** Gross break value moves with the
  matching tolerance (D3(c)): 175318 at ±1 day, 257418 at ±0. An alert threshold that shifts
  when someone tunes a constant is not a threshold. Per-loan absolute variance is
  matching-independent.
- **Value, not count.** Breaks exist in the sample today, so a count-based alert fires on day
  one and every day after until unrelated work lands, which is how an alert becomes background
  noise.

`DUPLICATE_SUSPECT` does not feed this alert — it carries no variance (D2(e)). Whether a
duplicate should raise its own separate signal is deliberately deferred: it is the week-5
idempotency gap, prevention is specified there, and a second alert this week would exceed
"one alert."

The threshold's value is a client decision and is not invented here; the pipeline fails closed
until it is set (open question, see Client Questions). Delivery is the operator's existing
channel consuming the exit code — no new alerting infrastructure.

### D5. ADR 0014

Records the decisions this spec makes: heuristic matching under a named successor, minor units,
break taxonomy, read-only posture, fail-closed abort, log-only correlation. Three or more
options per decision with rejection reasons, per the repo's ADR standard.

### D6. Documentation

`docs/runbook.md` gains the invocation, the exit codes, the meaning of each break class, and
what an operator does about each. The existing month-end entry
(`docs/runbook.md:119-120` — *"`reconciliation.peek` totals do not tie out and nothing runs on
a schedule. Finance reconciles by hand in a spreadsheet."*) is replaced rather than appended to.

---

## Out of Scope (Not This Week)

- **Preventing the double charge.** That is D19 — a client-minted idempotency key with a partial
  unique index — fully specified in ADR 0013 Decision 1 and `docs/spec-payments-week5.md` D1/D2.
  This week detects it. Saying "we can now see it" must not be mistaken for "it can no longer
  happen," and the client-asks draft states that explicitly.
- **Fixing the lost update (D3).** Atomic `UPDATE` plus `payment_applications`, specified in
  week-5 D3(d), slotted to W9 in `docs/plan-weeks7-10.md`.
- **The processor integration.** No capture call exists. Building one is a prerequisite the
  payments spec assumes silently; it is named here and owned there.
- **Exact matching on `processor_ref`.** Arrives with migration `0017_payments_idempotency`
  (renumbered from 0016). The heuristic matcher in D2(c) is explicitly interim and names its
  successor.
- **Persisting reconciliation results.** No table, no migration, no number taken from the
  payments sequence. The report is an artifact, not a record. Revisit when a second consumer
  exists.
- **Converting `payments.amount` / `balances.balance` off `DOUBLE PRECISION`.** D2 ledger work.
  Minor units are used *inside* this job only.
- **A scheduler.** See D3(a).
- **ACH returns, NACHA formats, chargeback handling.** Already out of scope in the week-5 spec
  and unchanged here.
- **Correcting the balances the report finds wrong.** D2(h). Remediation of the $500 on loan
  4471 is a business action, raised in the client-asks draft.
- **RBAC on the reconciliation endpoint.** `peek` inherits whatever `servicing-service` has
  today. D8 / ADR 0010 territory.

---

## Acceptance Criteria

Criteria 1–10 are what the implementation must satisfy. 11–13 are what the week itself delivers.

1. A charge and its downstream apply appear in the logs of both services under the same
   `request_id`, and a caller-supplied `X-Request-Id` is used verbatim.
2. A charge whose apply fails is distinguishable in the logs from one that succeeded, under
   that same id.
3. No log line asserts success before the work it describes has been performed.
4. Running the job over `db/settlement.csv` and the seeded `payments` rows at the ±1 day default
   reports exactly **5 breaks** — `MISSING_IN_LEDGER` 4 rows / 132100 minor,
   `REFUND_UNREPRESENTED` 1 row / 43218 minor, `MISSING_IN_SETTLEMENT` 0 — plus **1
   `DUPLICATE_SUSPECT`** (loan 5582, 41050 minor), and exits 1.
5. The job reports net variance, per-loan absolute variance, and gross break value, all three,
   in the same summary, each labelled with whether it depends on the matching tolerance.
5a. Re-running at a ±0 day tolerance changes the gross break value and the class counts but
   **not** the net variance (−88882) and **not** the `DUPLICATE_SUSPECT` result. This is the
   regression test for D2(d)/(e): a tolerance change must never silence the duplicate signal.
6. Removing or truncating the settlement file makes the job exit 2, not 0 and not 1.
7. A settlement row with an unparseable `amount` or an unknown `type` makes the job exit 2.
8. Every figure the job reports is an integer of minor units; no binary float appears in any
   comparison or sum.
9. The job performs no write. Verified by asserting the executed statements are all `SELECT`.
10. `GET /reconciliation/peek` returns the break summary, and `ledger_total` /
    `settlement_total` no longer exist.
11. ADR 0014 committed, 3+ options per decision with rejection reasons.
12. Runbook updated; the stale month-end entry replaced.
13. Every regression test watched fail before its fix, per the repo's prove-before-fix rule;
    `make prove` green from a detached worktree.

---

## Test Vectors

### Matching and classification

| ID | Input | Expected |
|---|---|---|
| V-MATCH | Ledger row (4471, 25000, 2026-06-01) and settlement `PR-100231` (4471, 25000, 2026-06-01) | matched; contributes 0 to variance |
| V-WINDOW | Ledger row dated 2026-06-01, settlement row dated 2026-06-02, same loan and amount | matched (±1 day) |
| V-WINDOW-OUT | Same pair two days apart | not matched; one break each side |
| V-DUP-ABSORBED | Two ledger rows (5582, 41050) on 06-01, settlement captures on 06-01 and 06-02, ±1d | **both match**; `MISSING_IN_SETTLEMENT` empty. Pins the behaviour in D2(d) so it cannot regress silently |
| V-DUP-DETECT | Same input | 1 × `DUPLICATE_SUSPECT` (loan 5582, 41050, gap 2s) regardless of the window |
| V-DUP-TOL-INVARIANT | Same input at ±0d and ±1d | `DUPLICATE_SUSPECT` identical in both; net variance identical in both; gross break value differs |
| V-MISSING-LEDGER | `PR-100290`, `PR-100311` (4471, 25000 each) with no ledger row | 2 × `MISSING_IN_LEDGER`, 50000 minor |
| V-REFUND | `PR-100299` (6011, 43218, `type=refund`) | 1 × `REFUND_UNREPRESENTED`, never `MISSING_IN_LEDGER` |
| V-SAMPLE | Full `db/settlement.csv` + seeded rows, June window, ±1d | 5 breaks (`MISSING_IN_LEDGER` 4 / 132100, `REFUND_UNREPRESENTED` 1 / 43218, `MISSING_IN_SETTLEMENT` 0), 1 `DUPLICATE_SUSPECT`, net −88882, per-loan absolute 175318, gross break value 175318, exit 1 |
| V-SAMPLE-TIGHT | Same at ±0d | 7 breaks (`MISSING_IN_LEDGER` 5 / 173150, `MISSING_IN_SETTLEMENT` 1 / 41050, `REFUND_UNREPRESENTED` 1 / 43218), gross 257418, net still −88882 |

### Fail-closed

| ID | Input | Expected |
|---|---|---|
| V-ABORT-MISSING | Settlement path does not exist | exit 2, message names the path; never exit 0 |
| V-ABORT-EMPTY | File exists, header only, zero data rows | exit 2 |
| V-ABORT-COLUMN | Header missing `amount` | exit 2 |
| V-ABORT-PARSE | One row with `amount=abc` | exit 2 |
| V-ABORT-TYPE | One row with `type=chargeback` | exit 2 — an unmodelled type is not silently dropped |
| V-CLEAN | Ledger and settlement agree exactly | exit 0 |

### Money

| ID | Input | Expected |
|---|---|---|
| V-MINOR | Amounts `0.10` and `0.20` on each side against `0.30` | ties out exactly; a float implementation of the same sum does not |
| V-DECIMAL-PARSE | Settlement `amount` of `250.005` | exit 2 — sub-cent precision is not silently rounded |

### Correlation

| ID | Input | Expected |
|---|---|---|
| V-TRACE | `POST /payments` with no header | one generated id present on the charge line, the outcome line, and servicing's apply line |
| V-TRACE-SUPPLIED | `POST /payments` with `X-Request-Id: abc123` | `abc123` used verbatim on all three |
| V-TRACE-DIRECT | `POST /accounts/{id}/apply-payment` called with no header | logged as `request_id=-`, not omitted |
| V-TRACE-FAIL | Servicing unreachable | the failure line carries the same id as the charge line |

### Read-only

| ID | Input | Expected |
|---|---|---|
| V-READONLY | Full job run against the seeded database | `payments`, `balances`, `payment_applications` byte-identical before and after |

---

## Verification

- `cd services/servicing-service && python -m pytest -q`
- `cd services/payment-service && python -m pytest -q`
- The blocking gates the change can touch: `redactor-drift`, `redaction-tests`, `secret-scan`,
  `migration-numbering-gate` (no migration is added, so this must stay trivially green),
  `doc-path-lint-tests`, `docs-drift`.
- `make prove` from a detached worktree for each regression test.
- The job run live against the compose stack, output pasted into the PR.

**Redaction note.** The report prints loan ids, amounts, dates, and processor references. It
must print no PAN, CVV, or SSN. `payments.pan` and `payments.cvv` are never selected. The
per-service redactor still applies to any log line the job emits; the report itself is stdout
and is not redactor-covered, which is why the column list is a review point rather than an
assumption.

---

## Client Questions

Raised in `docs/client-asks-2026-08-12-observability.md`; none blocks the build, each changes a
constant rather than a design.

- **Q1** Does the processor return a reference at capture, and in which field? Decides when
  exact matching replaces D2(c).
- **Q2** Is the handed-over file the full month? It covers 2026-06-01 to 06-07, 12 rows.
- **Q3** The variance is 28% of settled in this extract, against a stated "small and
  persistent." Which is representative?
- **Q4** What break value does finance accept as clean? Sets D4's threshold, which fails closed
  until answered.
- **Q5** Who receives the alert, and what is the month-end cut-off convention and timezone?
  The latter sets D2(c)'s tolerance, currently ±1 day by assumption.
- **Q6** Is the $500 on loan 4471 known and written off, or open? Business action either way.

---

## Status

Draft. ADR 0014 next. Implementation follows the repo's prove-before-fix rule: every regression
test is watched failing before the code that satisfies it exists.
