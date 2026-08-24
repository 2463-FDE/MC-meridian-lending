# 0020. Atomic payment application

Date: 2026-08-23

## Status

Proposed — built, not yet merged. This ADR records the decision; migration 0019, the
`balance.apply_payment` rewrite, both callers, the tests below, and the blocking
`atomic-apply-gate` are implemented in PR #77 (not stacked on this one), where
`test_lost_update.py` has flipped from failing to passing, proven by `make prove`. It
implements `docs/spec-payments-week5.md` D3(b), D3(d) and D3(e), and supersedes the
`captured_unapplied` half of ADR 0013 Decision 1 only where noted in Decision 3 below;
the rest of ADR 0013 stands. On `main`, none of this has landed — `balance.apply_payment`
is still the read-modify-write described below. Status becomes **Accepted** once PR #77
merges; update this line then to cite the merge commit and `atomic-apply-gate` rather
than the PR number (`docs/debt-log.md`'s status vocabulary).

## Context

Meridian credits a borrower's loan by overwriting one column. `balance.apply_payment`
reads `balances.balance`, subtracts in Python, and writes the result back — two
round-trips on a connection with autocommit on, with no row lock, no version column and
no transaction around the pair. Two applies to the same loan therefore read the same
opening figure and the second write erases the first.

This is measured, not theoretical. On 2026-08-02 one $100.00 intent sent eight ways
concurrently against the live stack produced eight `payments` rows: **$800.00 captured
and $600.00 credited. $200.00 was taken from borrowers and never applied to any loan,
and all eight responses were 200.** D19 (ADR 0013, migration 0018) closed the eight
charges. Nothing has closed the missing $200.

The business problem is that the platform cannot state what a borrower owes. A servicer
that takes money and does not credit it produces an overstated balance, a delinquency
that is not real, and — once that balance drives a late fee or a collection action — a
consumer-protection exposure well past the accounting error. There is no ledger to
reconstruct the truth from: `balances` is one mutable column with no history, so nothing
in the database records that a given payment was ever applied. Today applied-ness is
inferred from an HTTP status code between two services.

Two facts constrain the fix. Two handlers move this balance — `payment-service` and
`servicing-service` both write `payments` and both call the apply (ADR 0004, debt D23) —
so a lock taken in one service does not bind the other. And `db.py` hands every caller
one process-wide psycopg2 connection with autocommit on, so a fix that opens a
transaction in application code would span other threads' statements on the same
connection.

## Decision

**We will make the record of the application and the movement of the balance one
statement, and derive the amount and the loan from the stored payment row.**

Migration 0019 adds `payment_applications` — an append-only row per applied payment,
carrying `loan_id`, `payment_id`, `amount_minor`, with `UNIQUE (payment_id)`.
`balance.apply_payment` becomes a single writable-CTE statement: a `SELECT` over
`payments` joined to `balances` decides eligibility, an `INSERT ... ON CONFLICT
(payment_id) DO NOTHING` records the application, and an `UPDATE balances SET balance =
b.balance - (ins.amount_minor / 100.0)` moves the money from the value the INSERT
returned.

Four properties carry the decision:

1. The `UPDATE` computes from the stored value **inside** the statement. Under READ
   COMMITTED a concurrent updater blocks on the row lock and then re-evaluates against
   the committed row, so two applies serialize rather than overwrite. This is the fix
   for the $200.
2. The record and the movement are one statement, so neither can exist without the
   other, and no transaction is opened on the shared connection. The `JOIN` to
   `balances` is what makes "this loan has no balances row" produce zero eligible rows
   instead of an application row recording a movement that did not happen.
3. **Nothing the caller sends is a source of truth.** `amount` is removed from
   `ApplyPaymentIn` and from the body `payment-service` sends; the path's `loan_id`
   becomes a predicate the `SELECT` must match. The loan credited and the amount
   credited both come out of the `payments` row.
4. `UNIQUE (payment_id)` makes a replay a no-op rather than a second credit. The call
   returns `(balance, moved)` so a replay is a success that credited nothing.

**Decision 2 — legacy rows are backfilled, not left ineligible.** Migration 0018 added
`payments.amount_minor` but backfilled only `status`, so every pre-0018 row carries
`amount_minor = NULL` and the apply predicate refuses it. Migration 0019 backfills
`amount_minor = ROUND(amount::numeric * 100)::bigint WHERE amount_minor IS NULL`. The
cast goes through `numeric` before multiplying because `(amount * 100)::bigint`
truncates `12.34 * 100 = 1233.9999999999998` to 1233 and loses a cent.

**Decision 3 — the charge handlers finalize the status before applying, and `captured`
may only retire its idempotency key once an application row exists.** The apply credits
only a row whose status already says the card was captured, so the D19 order (apply,
then write the status) would make the predicate refuse every live payment. Both handlers
now write `captured` first and downgrade to `captured_unapplied` when the apply refuses.
That inversion opens a window — `captured` written, process dies, no application row —
in which D19's retirement rule would free the key and let a retry mint a second real
charge. `_RETIRE_SQL` in both services therefore requires a `payment_applications` row
before retiring a `captured` key. Applied-ness is a fact of the record; the status
string reports it.

This is a deliberate deviation from spec D3(c), which says there is no
`captured_unapplied` status because unapplied is the absence of a record. The status
stays: D19 shipped it, the 424 response contract and
`test_expired_captured_unapplied_never_retires_or_double_charges` depend on it, and
removing it is a second breaking change with no money defect behind it. The spec's
underlying requirement — one fact in one place — is met by making the record
authoritative and the status derived.

## Options considered

**Option A — `SELECT ... FOR UPDATE` around the existing read-modify-write.** The
smallest change: take a row lock, then keep the Python arithmetic. Rejected on two
counts. It requires an explicit transaction, and `db.py`'s single shared autocommit
connection means turning autocommit off would span concurrent requests' statements on
that connection — a correctness problem strictly worse than the one being fixed. And it
fixes only the lost update: nothing still records that a payment was applied, so a
replay credits twice and reconciliation still has nothing to compare against.

**Option B — an optimistic version column on `balances`.** Add `version`, re-read and
retry on a compare-and-swap miss. Rejected: it needs retry logic in application code on
the money path, it degrades under exactly the concurrency that produced the defect
(eight simultaneous applies means seven retries), and like Option A it leaves the apply
unrecorded. It also adds a mutable column to a table this decision is trying to stop
treating as the record.

**Option C — apply asynchronously through a queue, serialized per loan.** Rejected: no
queue or broker exists in this platform, adding one is a larger operational change than
the defect warrants, and it converts a synchronous money movement into one whose failure
the borrower's request cannot observe — trading a lost update for a silent backlog.

**Option D (chosen) — one statement, `payment_applications` as the record.** It fixes
the lost update through the row lock the database already provides, makes the apply
idempotent through a constraint rather than through code, and establishes the ledger
seam the week-5 scoping doc asks for without building a ledger.

**On Decision 2, two options were rejected.** Leaving legacy rows ineligible is the
smallest diff and is arguably honest — nobody applies a payment from before the schema
existed — but `db/init/002_seed.sql`'s seeded payment is one of those rows, so the demo
stack could not apply its own payment, and any real pre-0018 capture would be
permanently unappliable with no path to repair. Falling back to `amount` when
`amount_minor` is NULL was rejected because two money types in one statement is how the
float/minor-unit boundary becomes a defect instead of a boundary.

## Consequences

Captured money reaches the loan it was captured for, once, in the amount that was
captured. `payment_applications` is the first append-only money record in the platform:
reconciliation gains something to compare a processor capture against, and a future
ledger (D2) reads this table rather than re-modelling the movement.

The apply becomes refusable. A payment that is `processing`, `submitted`, absent,
belonging to another loan, or missing `amount_minor` now yields 422 and moves nothing,
where before any of those credited whatever the caller asked for. `payment-service`
reads that as not-applied and finalizes `captured_unapplied`, which is the honest state.
Callers of servicing's apply route must send `payment_id` only; the removed `amount`
field is a breaking change, and both callers move in this change.

`balances.balance` stays `DOUBLE PRECISION`. The `/ 100.0` in the `UPDATE` is the single
place minor units meet the float column, and converting the column is D2. The payment
waterfall (D14) is untouched — payments still go straight off principal, never fees then
interest then principal.

`adjust_balance` and `waive_fee` keep the unlocked read-modify-write shape on `balance`
and `past_due`. They are a different defect on a different path, carded in
`docs/debt-log.md` rather than fixed here, and
`test_the_other_mutations_are_still_a_separate_unlocked_read_then_write` pins that this
is a decision and not an oversight.

**Security.** The route stays internal-only behind `X-Internal-Service` (ADR 0014
Decision 1). Removing caller-supplied `amount` closes a money-creation path that the
authorization gate alone did not: an authorized internal caller could previously credit
any figure against any loan. No PII enters the new table.

**Performance and scalability.** One statement replaces two round-trips, so the apply
gets cheaper. Concurrent applies to the same loan now serialize on the row lock instead
of racing, which is a throughput ceiling per loan and the correct one — they were
previously "fast" by losing writes. Cross-loan concurrency is unaffected.
`payment_applications` grows one row per applied payment and is indexed by its unique
`payment_id`.

**Reliability and operations.** The crash window in Decision 3 leaves a `captured` row
with no application row. It is visible (the record is missing), it cannot double-charge
(the key does not retire), and it is what reconciliation reports. The runbook gains no
new manual step; an operator resolves such a row by re-driving the apply, which is now
idempotent.

**Testing.** `test_lost_update.py` documented the defect and failed on `main` from the
day it was written; on PR #77 it flips from failing to passing and keeps its
before-number, and its fixture models both statement shapes so a revert to the
read-modify-write turns it red again — `make prove` has run this on the implementation
commit and printed PROVEN. On `main`, the apply and balance write paths still run inside
the `backend` matrix's `|| true` suppression, so a regression there is silent on a green
build — the same condition that let the add-on-vs-actuarial APR defect survive. PR #77
closes that: it adds a blocking `atomic-apply-gate` running the D3 suites outside the
matrix, on the precedent of `tila-vectors-gate` and `reconciliation-gate`, with no
`continue-on-error`.

**Cost.** No new infrastructure, no new dependency, one new table.

## Implementation plan

1. Migration 0019 and the byte-identical `db/init/001_schema.sql` block, with the
   `amount_minor` backfill and definition assertions that `RAISE EXCEPTION` on a
   mismatch.
2. Readiness rungs in both services in the same change, probing the definition and
   schema-qualified with `current_schema()`.
3. `balance.apply_payment` as the single statement; `PaymentNotApplicable` for every
   refusal.
4. Both callers and the route contract in the same change — an endpoint-only fix leaves
   the defect reachable through servicing's own charge handler.
5. Decision 3's status ordering and the `_RETIRE_SQL` tightening, kept byte-identical
   across the two handler copies.
6. Tests, then `make prove` on the lost-update test.

## Rollback

Revert the application commits and leave migration 0019 applied. The table and the
backfilled `amount_minor` are additive: nothing outside the reverted code reads
`payment_applications`, and `amount_minor` was already written by both capture paths.
The reverted code returns to the unlocked read-modify-write and to accepting
caller-supplied amounts, so rollback reinstates the $200 defect — it is a way to restore
service, not a resting state. Rolling back only `payment-service` or only
`servicing-service` is not supported: the request body changed, and a
`payment-service` still sending `amount` against a servicing that requires
`payment_id`-only would have every apply refused, which fails closed (424, card charged,
balance unmoved) rather than crediting wrongly.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| A volume runs the code without migration 0019 | Readiness rungs in both services probe the table's definition; the service reports `schema_not_ready:payment_applications.*` instead of 500-ing the money path |
| A same-named `payment_applications` of the wrong shape already exists | The migration asserts every column type, every NOT NULL and the unique index, and `RAISE EXCEPTION`s on a mismatch — never `RAISE NOTICE ... skipping` |
| The backfill loses a cent on legacy rows | `ROUND(amount::numeric * 100)::bigint`, asserted by `test_the_migration_backfills_amount_minor_by_rounding_not_truncating`; the migration then refuses to finish while any `amount_minor` is still NULL |
| Decision 3's crash window frees an idempotency key and a retry double-charges | `_RETIRE_SQL` requires an application row before retiring a `captured` key, in both handler copies |
| The two handler copies of the retire rule drift | `test_the_claim_block_is_byte_identical_across_both_handlers` compares them |
| A regression lands silently under the `backend` matrix's `|| true` | The implementation PR adds `atomic-apply-gate`, running the D3 suites as a blocking job outside the matrix |
