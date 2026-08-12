# ADR 0014: Settlement Reconciliation as a Control, and Correlation on the Payment Span

- **Status:** **Proposed** — spec week. Implementation is scoped, not built.
- **Date:** 2026-08-12
- **Author:** Claude Code
- **Related:** ADR 0012 (Decimal / minor units precedent, applied here to the comparison),
  ADR 0013 (payment idempotency — prevents the duplicate this ADR only detects),
  ADR 0002 (single shared database — why the job reads `payments` directly),
  ADR 0004 (the decomposition that split capture from apply across two services).
  Debt D7 (no reconciliation job), D19 (no idempotency key), D3 (lost update),
  D2 (float money), D8 (servicing enforces no authorization).
- **Source:** `docs/spec-observability-week7.md`,
  `docs/client-asks-2026-08-12-observability.md`.

---

## Context

Meridian's finance team closes each month by adjusting the platform's number to the bank's
number. They do this because the platform cannot tell them why the two differ. The only
comparison that exists, `GET /reconciliation/peek`, returns two totals and nothing else; its own
source comment records that it is not a control.

The business cost is not the difference itself. It is that an unexplained difference is
indistinguishable from an explained one, so a real loss and a rounding artifact receive the same
treatment: a manual adjustment. Against the artifacts the client provided, that treatment has
been absorbing customer money. Two settled captures on loan 4471, totalling $500, have no ledger
row at all — the cards were charged and the balances never moved. Nobody saw this, because
nothing looks.

Three conditions make the difference invisible:

**The two totals are not comparable.** `ledger_total()` sums the whole `payments` table,
including roughly 600 rows dated from 2026-05-01 for loans outside the settlement file entirely.
The settlement file covers three loans over seven days. The subtraction produces a number before
any defect is considered.

**The comparison fails open.** `settlement_total()` returns `0.0` when the file is absent. In
the deployed configuration the path resolves, so this is latent — but it is a check that reports
a value for work it did not perform.

**Money is float.** `payments.amount` and `balances.balance` are `DOUBLE PRECISION`. Summing
them and asking whether the result ties out is unsound at the moment the answer matters.

Separately, the payment path spans two services — `payment-service` captures, `servicing-service`
applies — with no shared identifier. A capture whose apply fails produces one error line in one
service's log and no counterpart anywhere, and the caller still returns `captured`.

Reconciliation is out of scope in `docs/spec-payments-week5.md` by that spec's own statement, so
this decision does not overlap the payments work. It depends on it in one direction only: the
exact matching key arrives with migration `0017_payments_idempotency`, which is not built.

---

## Decision

### Decision 1 — Reconciliation matches rows individually and abstains where it cannot

We will replace the two-total comparison with per-row matching inside a bounded date window,
one-to-one on `(loan_id, amount_minor, settlement_date ± 1 day)`, classifying every unmatched
row. Where an identical tuple appears a different number of times on each side, the job reports
that a count differs and does not decide which row is the orphan.

| Option | Rejected because |
|---|---|
| A. Compare totals, as today | This is the current state and the reason finance adjusts to the bank. It cannot answer "why", which is the entire ask. |
| B. Match exactly on a shared reference | There is no shared reference. `payments` carries no processor reference, and no processor integration exists — `PROCESSOR_BASE_URL` is referenced by no code path. The column arrives with migration `0017`. This is the intended successor, not an option available now. |
| C. Match heuristically and pick a winner when counts differ | A one-to-one matcher that guesses which of two identical rows is the orphan states a fact it does not have. The report would be precise and sometimes wrong, which is worse for a control than being coarse and always right. |
| **D. Chosen: heuristic tuple, one-to-one, abstain on ambiguity** | Answers "why" with the data that exists, and its failure mode is a break the operator must read rather than a wrong attribution the operator cannot see. |

The window is a named constant. Its value is ±1 day because the ledger stamps `created_at` at
capture and the processor stamps `settlement_date` at settlement, and the client has not
confirmed a cut-off convention.

### Decision 2 — Duplicate detection does not depend on matching

We will detect duplicate captures by scanning the ledger alone for rows sharing
`(loan_id, amount_minor)` within a bounded time gap, before matching runs and independently of
its result.

This decision exists because simulating Decision 1 against the sample disproved the assumption
behind it. The ±1 day tolerance **absorbs** the duplicate: loan 5582's two rows two seconds
apart both match, the second pairing with the next day's settlement line, and
`MISSING_IN_SETTLEMENT` comes back empty. The matcher reports the defect the client asked us to
find as clean.

```
window ±0d:  MISSING_IN_LEDGER 5 (173150)  MISSING_IN_SETTLEMENT 1 (41050)  gross 257418
window ±1d:  MISSING_IN_LEDGER 4 (132100)  MISSING_IN_SETTLEMENT 0          gross 175318
             net −88882 under both
```

| Option | Rejected because |
|---|---|
| A. Tighten the window to ±0 days | Surfaces the duplicate by accident, and misclassifies every legitimately next-day settlement as two breaks — inflating the report with the noise this control exists to remove. It also leaves detection contingent on a constant nobody may tune again. |
| B. Accept that matching reports duplicates when it can | Makes the platform's answer to "are we double-charging" depend on a tolerance chosen for an unrelated reason. The answer would be "no" on this sample, and wrong. |
| C. Infer duplicates from settlement, not the ledger | The settlement file shows what the processor settled, which is one row. The duplicate exists on our side. Looking at the wrong side cannot see it. |
| **D. Chosen: independent ledger-side scan** | No window value can hide it, and the signal survives any later change to Decision 1. |

A duplicate carries no variance and is reported separately from breaks. Its money is already
counted on whichever side it landed; adding it to a variance figure would repeat the netting
error this ADR exists to remove.

### Decision 3 — The report states three figures and labels what each depends on

We will report net variance, per-loan absolute variance, and gross break value together, each
marked with whether it moves with the matching tolerance.

| Figure | Sample | Tolerance-dependent |
|---|---|---|
| Net variance | −88882 | No |
| Per-loan absolute variance | 175318 | No |
| Gross break value | 175318 at ±1d, 257418 at ±0d | **Yes** |

| Option | Rejected because |
|---|---|
| A. Report the net only | The net is −88882 against 175318 absolute. Netting hides roughly half the error and all of the causation. It is the reporting failure that produced "month-end is a little noisy". |
| B. Report net and gross without labels | Gross break value is partly an artifact of a constant we chose. Presenting it beside two stable figures invites a reader to treat it as a measurement. On this sample the two figures coincide at ±1 day, so the dependence is not visible in the output a reader actually sees. |
| **C. Chosen: three figures, each labelled** | A reader can tell which numbers survive a change to our own configuration. |

The alert in Decision 5 thresholds on per-loan absolute variance for this reason.

### Decision 4 — The job fails closed, with an abort distinct from a clean result

We will exit `0` for reconciled-no-breaks, `1` for reconciled-with-breaks, and `2` for ABORT —
the settlement file absent, unreadable, empty, missing a required column, or containing an
unparseable amount or an unmodelled type. `settlement_total()`'s `return 0.0` is removed.

| Option | Rejected because |
|---|---|
| A. Return zero totals on a missing file, as today | A control that reports a number for a comparison it did not perform is worse than no control: it produces evidence of cleanliness. |
| B. Treat an unreadable file as a break | Conflates "the data disagrees" with "we could not look". The operator responses differ — one is a finance investigation, the other is a broken mount. |
| **C. Chosen: three exit codes, abort is its own** | Mirrors `scripts/prove_test.sh` (PROVEN 0, REJECTED 1, ABORT 2, UNPROVEN 3), already the repository's convention for a verifier that must not claim what it did not check. |

An unmodelled settlement `type` aborts rather than being dropped. A silently ignored row is the
same defect as a silently missing file.

### Decision 5 — One alert, on the matching-independent figure

We will alert when per-loan absolute variance in the window exceeds a threshold expressed in
minor units, read from configuration with no default. The service fails closed until the
threshold is set, mirroring `disclosure-service/app/rules.py`.

| Option | Rejected because |
|---|---|
| A. Alert on break count | Breaks exist in the sample today, so the alert fires on day one and every day after until unrelated work lands. An alert that is always on is the noise it replaced. |
| B. Alert on gross break value | Moves when someone tunes the matching tolerance (Decision 3). A threshold that shifts with an unrelated configuration change is not a threshold. |
| C. Alert on net variance | Netting is the failure mode. Offsetting errors cancel and the alert stays quiet. |
| **D. Chosen: per-loan absolute variance, threshold from config, fail closed** | Matching-independent, does not cancel, and the value is the client's decision rather than ours. |

Duplicates do not feed this alert. Whether they warrant their own signal is deferred: prevention
is ADR 0013's, and a second alert would exceed the week's stated scope of one.

### Decision 6 — The correlation identifier is log-only

We will carry a `request_id` across the charge and the cross-service apply hop, accepted from an
`X-Request-Id` header or generated, logged by both services, and stored nowhere.

| Option | Rejected because |
|---|---|
| A. Add a `trace_id` column to `payments` | Duplicates work already specified. The persistent correlation between a capture and its application is `payments.id → payment_applications.payment_id` with `UNIQUE (payment_id)`, specified in `docs/spec-payments-week5.md` D3(b). A second persistent key either duplicates it or competes with it. |
| B. Add the id to the apply request body | The body is the payments work's territory — its D3(d) removes `amount` from `ApplyPaymentIn` and makes `payment_id` the only input. Changing the same structure here creates a merge conflict in a money path. A header does not. |
| C. Adopt an instrumentation framework | No OpenTelemetry, Prometheus, or statsd exists anywhere under `services/`. Introducing one to correlate two log lines is a dependency and an operational surface for a problem two log fields solve. Revisit when a third service joins the span. |
| **D. Chosen: header-propagated `request_id`, log-only** | Mirrors `decision-service`'s existing convention rather than inventing a second vocabulary, and leaves the persistent seam to the work that owns it. |

Servicing logs `request_id=-` when the header is absent, so an uncorrelated direct call is
visible as such rather than indistinguishable from a correlated one.

### Decision 7 — The job is read-only

We will issue `SELECT` only. The job does not correct a balance, insert a payment, or write any
table.

| Option | Rejected because |
|---|---|
| A. Correct balances the report finds wrong | Unauthorized money movement. Servicing enforces no authorization (D8), there is no maker-checker, and the write would land on the unlocked read-modify-write that is D3 — so an automated correction races the same defect that produced the discrepancy. |
| B. Write a suggested correction for an operator to approve | Requires a table, a migration, and a review workflow. The migration would take a number from the payments sequence, which was just renumbered to `0017`–`0020`. Out of proportion to a first control. |
| **C. Chosen: read-only, remediation stays a business action** | The $500 on loan 4471 needs a human decision about two customers, not an automated `UPDATE`. |

---

## Consequences

### Positive

- Finance receives named causes with row-level evidence instead of a single unexplained
  difference. "We adjust to the bank number" becomes four classified figures.
- The $500 captured and never credited on loan 4471 becomes visible, and stays visible daily.
- The duplicate capture is detected by a check no configuration change can silence.
- A run that could not read its input can no longer report a clean result.
- The two halves of the payment path become traceable to one another in the logs, including the
  case where the apply fails and the caller still reports `captured`.

### Negative / tradeoff (accepted)

- **Matching is inexact and we say so.** Loans with repeating equal instalments are the common
  case in an installment portfolio, so ambiguous tuples will be routine. The report abstains
  rather than guessing, which means some breaks require manual review until migration `0017`
  supplies the exact key.
- **The gross break value is partly an artifact.** Labelled, but a reader who ignores the label
  can still misread it.
- **Detection without prevention.** The report finds double charges every day until ADR 0013's
  idempotency key ships. Communicated to the client explicitly; the risk is that "we can see it"
  is heard as "it is fixed".
- **No schedule.** "Daily" is an operational convention plus the operator's existing cron. If
  nobody runs it, the control is a script.
- **The report is an artifact, not a record.** Two runs a month apart cannot be compared by the
  platform. Accepted until a second consumer exists.
- **`ledger_total()` and `settlement_total()` are removed.** Any caller outside this repository
  breaks. `GET /reconciliation/peek` is the only known caller.

### Neutral

- Minor units are used inside the job only. `payments.amount` and `balances.balance` stay
  `DOUBLE PRECISION`; converting them is D2 ledger work.
- The `±1 day` window and the duplicate gap bound are configuration, not constants in a
  comparison, so the client's cut-off answer changes a value rather than the design.

---

## Cross-cutting concerns

**Security.** The report prints loan ids, amounts, dates, and processor references. It selects
neither `payments.pan` nor `payments.cvv`, so cardholder data cannot reach the output. The
report is stdout and is therefore *not* covered by the per-service PII redactor — the column
list is the control, which is why it is a stated review point rather than an assumption. Log
lines the job emits remain redactor-covered.

**Performance.** Matching is over one bounded window, not the full history — the sample is 12
settlement rows against 7 ledger rows. An installment portfolio's month is thousands, not
millions. A linear scan is adequate; the duplicate check is over the same windowed set.

**Scalability.** In-memory matching bounds the job by window size. If a window ever exceeds
memory, the fix is a narrower window before it is a different algorithm.

**Reliability.** The abort path is the reliability property: degraded infrastructure produces
exit 2, never exit 0. The job holds no state between runs, so a failed run has no cleanup.

**Maintainability.** Extends `services/servicing-service/app/reconciliation.py` rather than
adding a parallel module, and removes the weaker comparison instead of leaving it beside its
replacement — the drift that three hardcoded fee constants demonstrated.

**Cost.** No new dependency, no new service, no new infrastructure. `boto3`-style optional
extras are not involved.

**Operational impact.** One new command an operator runs and one runbook section. The stale
month-end entry is replaced, not appended to. The alert has no delivery mechanism of its own —
it consumes the exit code through whatever channel the operator already has, which is a
deliberate limitation and is stated to the client.

**Testing impact.** Every break class, every abort path, and the tolerance-invariance of the
duplicate signal are pinned as test vectors, including a vector asserting the *absorbed*
duplicate behaviour so it cannot regress into looking correct. The read-only property is
asserted by inspecting the executed statements.

---

## Implementation plan

1. Duplicate detection and the break classifier as pure functions over parsed rows, with the
   sample as the fixture. No database, no file system.
2. Settlement parsing with the abort paths, including the unmodelled-type case.
3. Minor-unit conversion, and the three variance figures.
4. Replace `ledger_total` / `settlement_total`; rewrite `GET /reconciliation/peek` onto the new
   path and delete its "not a real control" comment.
5. The module entrypoint, exit codes, JSON on stdout, summary on stderr.
6. `request_id` on the charge path, the header on the apply hop, servicing reading it, and the
   success line moved after the work it describes.
7. The alert threshold in configuration, failing closed when unset.
8. Runbook.

Every regression test is watched failing before the code that satisfies it exists, per the
repository's prove-before-fix rule; `make prove` runs from a detached worktree.

---

## Rollback strategy

Steps 1–5 add a command and rewrite one endpoint. Reverting the commit restores the previous
`peek`; nothing persists, so there is no data to unwind and no migration to reverse.

Step 6 changes log content and adds one outbound header. Servicing defaults the header to `-`,
so a partial rollback in either direction leaves both services running — a rolled-back
`payment-service` sends no header and servicing records `-`; a rolled-back servicing ignores a
header it does not read.

The alert is configuration. Unsetting the threshold returns the service to fail-closed on that
path only.

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Ambiguous tuples are common in an installment portfolio, so many breaks need manual review | The report abstains rather than guessing; migration `0017`'s `processor_ref` is the named successor, and Decision 1 is written to be replaced |
| A reader treats gross break value as a stable measurement | Labelled in the report itself, not only in this ADR |
| The client hears detection as prevention | Stated in `docs/client-asks-2026-08-12-observability.md` and repeated in the runbook |
| Nobody runs the job, so the control exists on paper | Exit codes make it CI-runnable; the runbook names the invocation. A scheduler is deferred, not assumed |
| The cut-off convention turns out to differ from ±1 day, changing every classification | The window is configuration; the client question is open and the answer changes a value |
| Someone tightens the window and believes duplicate detection improved | Decision 2 makes duplicate detection independent of the window, and a test vector asserts invariance across both values |
| The report leaks cardholder data | `pan` and `cvv` are never selected; the column list is a review point |

---

## Assumptions challenged

**"The month-end difference is small and persistent."** It is 28% of the settled amount in the
extract provided. Either the extract is unrepresentative or the description is. Raised with the
client; it is the load-bearing claim in the pre-read.

**"A tighter matching window is more accurate."** It is not. ±0 days surfaces the duplicate by
accident while misclassifying legitimate next-day settlements. Accuracy came from a second,
independent check — not from tuning the first one.

**"The processor reference is the missing piece."** It is missing, but so is the integration
that would supply it. The payments spec assumes a processor call in its ordering guarantee, and
no such call exists in any code path. Recorded here because that spec does not say it.

**"Reconciliation should correct what it finds."** Rejected. The correction is customer
remediation, and the write would race D3.

---

## Sign-off status

Proposed. Depends on no unmerged branch. Client answers on the alert threshold and the cut-off
convention change configured values, not the decisions above.
