# Spec: Self-Serve Payments — Idempotency and PCI Scope (Week 5)

**Owner:** Dana (VP Lending Ops)
**Date:** 2026-08-02
**Status:** **Spec only.** Implementation is scoped, not built. No code ships this week.
**Source brief:** Dana's request — *"Customers want to pay online — card and ACH. Let's just
add a payment form so they can self-serve. We've had a few 'I was charged twice' complaints,
but I think people are just confused. Keep it simple."*
**Scoping:** `docs/scoping-payments-week5.md` — the reframe, ground-truth verification of the
handover, and the three-phase access model. Read it first; this document assumes it.
**Related:** ADR 0013 (this week's decision record, supersedes ADR 0003), ADR 0010
(ownership authorization — the continuation-token pattern reused here), ADR 0012
(Decimal/minor units precedent), ADR 0004 (the decomposition that duplicated the charge
handler). Debt D19, D13, D5, D8, D2, D20.
**Branch:** `feature/payments-week5`, cut from `main` @ `170ed29`.

---

## Executive Summary

Specify a payment path that cannot double-charge and cannot hold prohibited cardholder data,
then let the form sit on top of it.

Three duplicate-charge complaints are not confusion, and this is measured rather than
argued. `scripts/repro_double_charge.py`, run against the live stack on code byte-identical
to `main`, turns one $100.00 payment intent into **eight charges totalling $800.00, all
returning `200`** — and credits only $600.00 of it to the loan. The fix is a client-minted
idempotency key arbitrated by a **unique index**, not by application logic, because there
are **two** live charge handlers writing the same table
(`payment-service/app/payments.py:76` and `servicing-service/app/payments.py:75`) and a
constraint binds writers that application code does not know about.

The missing $200 is a second, independent defect the reproduction exposed: the balance
mutation is an unlocked read-modify-write, so concurrent applies overwrite each other. An
idempotency key does not fix it. Both defects compound against the customer — charged more,
credited less — so the balance mutation must become atomic in the same change.

The same request stores the full PAN and the CVV. **Retaining the CVV after authorization is
a flat prohibition**, not a control gap, and the remediation is a deletion. A self-serve form
is simultaneously the first browser in Meridian's payment path and therefore the first
chance for the PAN never to reach a Meridian server at all — so the correct design *shrinks*
the assessment footprint that the naive one would permanently grow.

Self-serve access does not need borrower accounts. This repo already ships a hardened
capability-token pattern (ADR 0010 Phase B, `db/migrations/0008`+`0009`); swapping the noun
from `application:{id}` to `pay:loan:{id}` inherits the hashing, expiry, and cookie
indirection rather than rebuilding them.

Deliverables are documents: this spec, ADR 0013, and the test vectors they specify — measured
red where a live run can show it, committed as executable tests in the build week. Nothing is
built this week.

## Problem Statement

**Surface request (Dana):** add a payment form; the double-charge complaints are probably
confusion; keep it simple.

**Real problem, verified against `main`:**

- **Duplicate charges are real, and reproduced.** No idempotency key is accepted or stored
  (`payment-service/app/routers/payments.py:27`, `app/schemas.py` `PaymentIn`), and the DDL
  says so (`db/init/001_schema.sql:121` — `-- no idempotency_key, no unique(charge_ref)`).
  `scripts/repro_double_charge.py` against the live stack, loan 4471, one $100.00 intent:

  | Case | Rows created | Captured | Credited |
  |---|---|---|---|
  | 2 sequential POSTs (the retry in the logs) | 2 | $200.00 | $200.00 |
  | 8 simultaneous POSTs | 8 | $800.00 | **$600.00** |

  Every attempt returned `200`.
- **Concurrent applies lose money, independently of idempotency.** The $200 gap above is
  the unlocked read-modify-write in `balance.apply_payment`, annotated as such in the code
  (`servicing-service/app/main.py:80-85`). Concurrent applies read the same opening balance
  and overwrite one another; the loss is non-deterministic (a 5-way run lost one
  application, an 8-way run lost two). A unique index on the idempotency key removes the
  *trigger* by collapsing duplicate intents, but two genuinely distinct concurrent payments
  still race. This needs its own fix (D3d).
- **Two handlers, one table.** ADR 0004 copied the charge handler into `payment-service`
  and left the original routed in `servicing-service`. Dedupe in one leaves the other open.
- **A silent third failure mode nobody has reported.** `_apply_via_servicing`
  (`payment-service/app/payments.py:92-110`) catches every exception at `:104` and the
  caller still returns `"status": "captured"` at `:87`. A servicing timeout yields: card
  charged, row written, balance possibly unmoved, success returned. No reconciliation closes
  it. Support will never describe this correctly, so it will never be fixed reactively.
- **Prohibited data at rest.** `cvv TEXT` (`db/init/001_schema.sql:117`) plus both INSERTs
  retain sensitive authentication data for every payment since go-live. `pan TEXT` is
  plaintext with no tokenization. The seed writes real-looking PANs and CVVs into `payments`
  (`db/init/002_seed.sql:68-74`) and a PAN into `audit_logs.detail` (`:79`).
- **An internal endpoint is publicly routed.** `POST /lss/accounts/{id}/apply-payment`
  (`servicing-service/app/main.py:80`) reduces a balance, is reachable by any authenticated
  session through the gateway, and has no `X-Internal-Service` gate — unlike the three
  sibling services that do. It never checks that a payment was captured. That is money
  creation, and this feature calls that endpoint directly.
- **ACH is modelled as a card.** One verb, `charge()`, returns `"status": "captured"` for
  both rails, selected by a `method` string defaulting to `"card"`. An ACH submission is not
  funds received; treating it as such makes the balance wrong for every payment that later
  returns.

**Already fixed, contrary to the handover:** the CVV is no longer logged.
`_redacted_charge_req` (`payment-service/app/payments.py:43-72`) masks PAN, CVV, and SSN at
the value level before interpolation and omits the client-controlled `name` field. The
handover's claim came from a stale module docstring (`payments.py:1-9`,
`app/main.py:1-7`), which this spec corrects. The remaining exposure is **storage**, not
logging — and it is the higher-severity one.

**Known, deferred:** RBAC on the servicing layer (D8 / ADR 0010) and the ledger (D2). See
*Out of Scope*.

---

## Deliverables (In Scope)

### D1. Idempotency-key contract

**Key.** `Idempotency-Key` request header on `POST /payments`. Client-minted UUIDv4.
**Required** — a request without one is refused. The server cannot distinguish a retry from
a new intent; only the client knows. The frontend mints one key per *form submission* and
reuses it across every transport-level retry of that submission; a fresh user-initiated
payment mints a new key.

**Fingerprint.** The server stores `sha256` over a canonical serialization of
`(loan_id, amount_minor, method, card_token)`. This is what distinguishes a genuine replay
from a client bug that reused a key with different parameters.

**Behaviours.**

| Situation | Response |
|---|---|
| No `Idempotency-Key` header | `400`, no row written, no processor call |
| Malformed key (not a UUID) | `400`, no row written, no processor call |
| First use | Process normally; `201` |
| Replay — same key, same fingerprint, prior request terminal | **The original status code and body, byte for byte**, plus `Idempotent-Replay: true`. That header is the only difference, so a client ignoring it sees an identical result. |
| Replay — same key, **different** fingerprint | `422`, no second row, no processor call. This is a client defect, not a retry. Follows `draft-ietf-httpapi-idempotency-key-header`. |
| Concurrent — same key, prior request not yet terminal (`processing`, or ACH `submitted`) | `409` with `Retry-After`. Exactly one processor call happens. |
| Same key after the retention window, prior request terminal | Treated as a new payment |
| Same key after the retention window, prior request **not** terminal | `409`, as above. The window releases a finished payment's key; it is not a timeout on the payment itself. |

**Retention.** Keys are honoured for `PAYMENT_IDEMPOTENCY_TTL_HOURS`, default **24**. A
customer clicking "pay" the next day is a new intent, not a retry. Configurable, so a
different answer from Dana (Q5) costs a config change.

Expiry is a state transition on the row, not a property of the index — a unique index cannot
be time-scoped, because `now()` is not immutable and cannot appear in its predicate. The
insert stamps `idempotency_expires_at = now() + PAYMENT_IDEMPOTENCY_TTL_HOURS`. Once that
passes **and the payment has reached a terminal state** (D5), the key is **retired**:
`idempotency_key` is set to `NULL`, which drops the row out of the partial index
(`WHERE idempotency_key IS NOT NULL`) and frees the value for reuse. An intent still in flight
keeps its key however old it is — an ACH payment is `submitted` for days, and releasing its
key would let a second payment claim it while the first is still live. The payment row is
never deleted or archived; only the key it holds is released, so the charge history stays
intact and a retired row is the same shape as the pre-migration rows the partial index already
tolerates. D2 defines when the transition fires and what makes it safe under concurrency.

The cost of this follows from the row above and is deliberate: past the window, a genuinely
late retry of the *same* intent is charged again, because the system has by then given up the
evidence that would tell it from a new payment. That is what "treated as a new payment" means.
`PAYMENT_IDEMPOTENCY_TTL_HOURS` is the lever if Dana wants that window wider.

**Replay bodies are derived, not stored.** The response is reconstructed deterministically
from the persisted row. No response-snapshot column — it would be a second source of truth
for the same facts.

**A distinct key is forwarded to the processor.** The value passed on the processor API call
is `processor_idempotency_key`, generated once per `payments` row — **not** the client's
Meridian `Idempotency-Key`. This is deliberate. If Meridian forwarded the client key verbatim,
the two idempotency windows would be coupled across two vendors we do not co-version: once a
Meridian key is retired after `PAYMENT_IDEMPOTENCY_TTL_HOURS` (D1), a legitimately *new*
payment reusing that key would be sent to the processor under the same value, and a processor
whose own retention outlives Meridian's would collapse the new charge as a replay of the old
one — Meridian writes a second row and may move the balance again while the processor silently
suppresses the charge, or vice-versa. Version-skew between the two retention windows becomes a
money-movement bug.

Binding the processor key to the row instead breaks that coupling: a post-expiry "new payment"
is a new row (R6) with a new `processor_idempotency_key`, so the processor treats it as
distinct exactly as Meridian does. The duplicate-escape protection the forwarded key was there
for is preserved without the coupling — the processor key is *stable per row*, so a Meridian
replay (which never reaches step 3, it short-circuits at replay) and the stuck-row reaper both
address the processor with the same value for the same charge, and the processor still collapses
a true retry of *that* charge. The one property we drop — that a client reusing an expired
Meridian key is de-duplicated at the processor — is the property D1 explicitly gives up:
past the window a late retry of the same intent is a new payment by definition.

### D2. Database uniqueness and the transaction boundary

The correctness guarantee lives in the schema, not in a service. Two handlers write this
table today, and a support engineer with `psql` is a third.

`db/migrations/0012_payments_idempotency.sql` (and the matching edit to
`db/init/001_schema.sql`, which this repo keeps in step):

```sql
ALTER TABLE payments
  ADD COLUMN IF NOT EXISTS idempotency_key         TEXT,
  ADD COLUMN IF NOT EXISTS idempotency_expires_at  TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS request_fingerprint     TEXT,
  ADD COLUMN IF NOT EXISTS status                  TEXT NOT NULL DEFAULT 'captured',
  ADD COLUMN IF NOT EXISTS processor_idempotency_key TEXT,
  ADD COLUMN IF NOT EXISTS processor_ref           TEXT,
  ADD COLUMN IF NOT EXISTS amount_minor            BIGINT,
  ADD COLUMN IF NOT EXISTS updated_at              TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS payments_idempotency_key_uniq
  ON payments (idempotency_key) WHERE idempotency_key IS NOT NULL;
```

`processor_idempotency_key` is a **distinct** value from the client's Meridian
`idempotency_key`, generated once per `payments` row (D1, "forwarded to the processor")
and sized/namespaced to the processor's contract rather than to Meridian's. It is not made
unique in the schema — the processor enforces its own uniqueness — but it is stamped at
insert time so the stuck-row reaper can re-query the processor for a row that crashed before
`processor_ref` was written, independent of whether the Meridian key has since been retired.

`status` defaults to `'captured'` because that is what every pre-migration row factually is;
the partial index leaves those legacy `NULL` keys untouched and non-conflicting. `amount_minor`
follows the ADR 0012 precedent — integer minor units for anything this design adds. The
existing float `amount` column is left alone (D2 stays open elsewhere).

`idempotency_expires_at` is what makes D1's retention window enforceable rather than a claim.
The index is deliberately unconditional on time: it cannot carry a `now()` predicate, so the
window has to be a column the retirement transition below reads. Without that column the index
is permanent, the second request in R6 conflicts forever, and the contract's last row is
unimplementable — the defect this revision closes.

**The conflict target must carry the index predicate.** The arbiter is a *partial* unique
index (`WHERE idempotency_key IS NOT NULL`), so a bare `ON CONFLICT (idempotency_key)` matches
no arbiter and Postgres raises `there is no unique or exclusion constraint matching the ON
CONFLICT specification` at runtime — the insert fails before it can claim the key, disabling
the whole double-charge control. Every insert against this index must therefore spell the
predicate: `ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING`. This
is not cosmetic: it is the difference between the control working and the control raising on
first use. The migration smoke test in the test-vector table (`R-DDL`) executes this exact
insert against a real Postgres so the mismatch cannot ship.

**Claim the key before contacting the processor.** The write is insert-first, never
read-check-then-insert:

```
1. INSERT ... (idempotency_key, idempotency_expires_at, request_fingerprint,
               processor_idempotency_key, status='processing')
   ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
   RETURNING id
2. zero rows  -> a row already holds this key. Read it.
   2a. its idempotency_expires_at <= now() AND its status is terminal (D5)
       -> the key is expired. Retire it in one statement:
         UPDATE payments SET idempotency_key = NULL
          WHERE idempotency_key = $1
            AND idempotency_expires_at <= now()
            AND status IN ('captured','failed','settled','returned')
       Then retry step 1 exactly once. If that retry also conflicts, another caller
       claimed the freed key first -> fall through to 2b against the new row.
   2b. otherwise -> a live key, or an expired one on an intent still in flight.
       Branch per D1's table.
3. one row    -> we own this intent. Call the processor, passing this row's
                 processor_idempotency_key (NOT the client's Meridian key).
4. UPDATE the row to its terminal status + processor_ref.
5. Apply to servicing (D3).
```

Two concurrent identical requests both reach step 1; exactly one gets a row. The other
cannot proceed to step 3, so **the processor is contacted once**. This is the property no
amount of application-level checking provides, and it is why the constraint is the design
rather than a backstop.

The same holds at the expiry boundary, which is the point of doing retirement this way rather
than by reading the timestamp and deciding in the service. Step 2a's `UPDATE` is a single
statement whose `WHERE` stops matching the moment the key is `NULL`, so concurrent
expired-key requests serialize on that row: under `READ COMMITTED`, the second one re-evaluates
its predicate against the committed row, matches nothing, and updates zero rows. Exactly one
caller can therefore go on to win step 1. The losers land in 2b and get D1's concurrent answer
— the same `409` a live key produces. Retirement frees the key without opening a window in
which two requests each believe they own it. The retry is bounded at one, so no request can
loop.

**Only a terminal intent releases its key.** The status condition in 2a is not defensive
padding. An ACH payment sits `submitted` for days (D5), routinely outliving a 24-hour window,
and a `processing` row can outlive it after a crash. Retiring the key of either one would do
two things wrong at once: it would free the key for a *new* payment while the original intent
is still live, reintroducing exactly the duplicate charge this deliverable closes; and it would
destroy the value the stuck-row reaper resolves that row by, since the key is what it queries
the processor with. So an unfinished intent keeps its key past the window and answers 2b —
`409`, not a new charge. The window governs how long a *finished* payment's key stays claimable
for replay, not how long the system is willing to wait for the payment.

**Stuck-row resolution.** A crash between steps 3 and 4 leaves `processing`. A reaper
resolves rows older than `PAYMENT_PROCESSING_TIMEOUT_MINUTES` by querying the processor for
that row's `processor_idempotency_key` — the same value step 3 charged under, stamped at
insert so it survives even a crash before `processor_ref` is written and independent of any
later Meridian-key retirement. Rows terminal-but-unapplied (see D3) are retried by the same
job.

The same job retires expired keys in bulk, under the same terminal-only condition as step 2a
and for the same reason — it must not strip the key off a row it is itself about to resolve
by that key:

```sql
UPDATE payments SET idempotency_key = NULL
 WHERE idempotency_key IS NOT NULL
   AND idempotency_expires_at <= now()
   AND status IN ('captured','failed','settled','returned');
```

Ordering within the job follows from that: resolve stuck rows first, retire keys second, so a
row that reaches a terminal status in this pass has its key released in the same pass rather
than a cycle later. Step 2a is then an optimization for whichever request arrives first, not
the only thing keeping keys from being held past their window by clients that never retry.

### D3. The cross-service apply hop

**(a) The endpoint becomes internal-only.** `POST /accounts/{loan_id}/apply-payment` requires
`X-Internal-Service`, matching `kyc-service/app/routers/kyc.py`,
`decision-service/app/routers/decisions.py`, and `disclosure-service/app/routers/offers.py`.
The gateway does not forward the header from a client. This closes the money-creation path
described in the problem statement. It is not RBAC and needs no identity model.

**(b) The applied fact becomes a record, not a status.** `db/migrations/0013_payment_applications.sql`:

```sql
CREATE TABLE IF NOT EXISTS payment_applications (
    id           SERIAL PRIMARY KEY,
    loan_id      INTEGER NOT NULL REFERENCES loans(id),
    payment_id   INTEGER NOT NULL REFERENCES payments(id),
    amount_minor BIGINT  NOT NULL,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT payment_applications_payment_uniq UNIQUE (payment_id)
);
```

Append-only, enforced by the same `BEFORE UPDATE OR DELETE OR TRUNCATE` trigger pattern
already used for `decision_events`.

`apply_payment` inserts here **before** mutating the balance, and the row it inserts is read
out of `payments` rather than taken off the request: the write is an `INSERT ... SELECT` over
the referenced payment with `ON CONFLICT (payment_id) DO NOTHING`. Zero rows means the payment
was already applied *or* is not eligible to apply, and D3d below separates those two. The
`payment_id` that `payment-service` already sends and servicing currently ignores
(`servicing-service/app/main.py:80-85`) becomes both the dedupe key and the row the loan and
the amount are read from — the cheap half of the cross-service problem was already plumbed.

This is the **ledger seam** required by the scoping doc §3.4: applications are events, they
add no new mutable money column, and a future ledger reads this table rather than
re-modelling. The balance mutation itself stays as it is; converting it is D2 work.

**(c) A failed apply is never reported as success.** The `except Exception` at
`payment-service/app/payments.py:104` no longer returns a bare `"captured"`. The response
reports the payment status *and* whether it has been applied, derived from the presence of
a `payment_applications` row. There is no `captured_unapplied` status — unapplied is the
absence of a record, which keeps one fact in one place.

**(d) The balance mutation becomes atomic.** This is a separate fix from everything above
and the reproduction is why it is in scope: 8 concurrent applies captured $800.00 and
credited $600.00.

`balance.apply_payment` currently reads the balance, computes, and writes it back
(`servicing-service/app/main.py:80-85`, annotated *"still does the unlocked
read-modify-write"*). Concurrent callers read the same opening value and the last writer
wins. Required instead:

```sql
BEGIN;
  INSERT INTO payment_applications (loan_id, payment_id, amount_minor)
  SELECT p.loan_id, p.id, p.amount_minor
    FROM payments p
   WHERE p.id = :payment_id
     AND p.loan_id = :loan_id           -- the path's loan is a claim to check, not an input
     AND p.amount_minor IS NOT NULL
     AND p.status IN ('captured', 'settled')
  ON CONFLICT (payment_id) DO NOTHING
    RETURNING loan_id, amount_minor;    -- zero rows => already applied, or ineligible

  UPDATE balances
     SET balance = balance - (:applied_amount_minor / 100.0)  -- what the INSERT returned
   WHERE loan_id = :applied_loan_id                           -- likewise
    RETURNING loan_id;                  -- zero rows => no balances row, ROLLBACK
COMMIT;
```

Four properties matter and none is optional:

1. The `UPDATE` computes from the stored value inside the statement, so concurrent updates
   serialize on the row lock instead of overwriting each other.
2. The application record and the balance movement share one transaction, so the record
   cannot exist without the movement or vice versa. Today they are two unrelated writes
   in two services.
3. **Nothing the caller sends is a source of truth.** The request body carries a `payment_id`
   and nothing else — `amount` is removed from `ApplyPaymentIn`
   (`servicing-service/app/main.py:74-77`) and from the body the one caller sends
   (`payment-service/app/payments.py:96`, `json={"amount": ..., "payment_id": ...}`), and the
   path's `loan_id` becomes a predicate the `SELECT` has to match rather than a value any
   write uses. The loan credited and the amount credited both come out of the `payments` row.
   Without this, an internal or reaper caller applies payment A to loan B, or credits an
   amount that was never captured, and the append-only table makes either permanent.
4. **Each statement must affect exactly one row, and zero is a rollback.** Zero from the
   `INSERT` is two different facts and the code has to tell them apart before answering:
   either a `payment_applications` row already exists for this `payment_id` (already applied
   — commit, move nothing, return the current balance), or the `SELECT` matched nothing (no
   such payment, the payment belongs to a different loan, `amount_minor` is `NULL`, or the
   status is not one that credits — `ROLLBACK`, refuse, move nothing). Telling the two apart
   takes one read of `payment_applications` by `payment_id`; `payments.loan_id` being `NULL`
   needs no separate predicate, since a `NULL` cannot equal the path's loan. Zero from the
   `UPDATE` means the loan has no `balances` row: `ROLLBACK`. `balances.loan_id` is the
   primary key (`db/init/001_schema.sql:106`), so more than one row is impossible and zero is
   the case that matters — without the check, the application row records an apply that moved
   no money while `UNIQUE (payment_id)` blocks the correct retry for good.

The `/ 100.0` is the single place minor units meet the float column.
`payment_applications.amount_minor` is the integer of record; `balances.balance` stays
`DOUBLE PRECISION` until D2 converts it, and the conversion sits at that one write instead of
spreading along the path.

Restricting the status to `captured` and `settled` is also what makes an ACH `submitted`
payment move no balance (acceptance criterion 8) a property of the write rather than of the
caller's discipline.

**(e) Both apply paths go through the record.** `balance.apply_payment` has two callers, not
one: the endpoint above, and servicing's own charge handler calling it in-process
(`servicing-service/app/payments.py:79` — the second of the two duplicate handlers). Convert
only the endpoint and that handler still moves a balance with no `payment_applications` row
and no lock, so the record is not the record of *every* apply and A6 stays red for anything
posted to servicing directly. The transaction in D3(d) therefore belongs inside
`balance.apply_payment`, keyed on a `payment_id` every caller supplies: servicing's handler
captures the id with `INSERT ... RETURNING id` (it currently discards it,
`servicing-service/app/payments.py:73-77`) and passes it in. Consolidating the two handlers
stays out of scope — sharing the write is not the same as merging the routes.

**Not in scope here:** converting `balances.balance` off `DOUBLE PRECISION`, and the ledger
itself. This is the minimum that stops captured money going unapplied while D2 waits.

### D4. PCI: no SAD, tokenized PAN

**(a) Sensitive authentication data is deleted, not merely unwritten.**
`db/migrations/0014_payments_detokenize.sql`, in this order:

```sql
UPDATE payments SET cvv = NULL, pan = NULL;   -- clear live tuples
ALTER TABLE payments DROP COLUMN cvv;
ALTER TABLE payments DROP COLUMN pan;
VACUUM FULL payments;                          -- rewrite the heap
```

**The `VACUUM FULL` is not optional and is the step most likely to be skipped.** Postgres
`DROP COLUMN` only marks the attribute dropped; the bytes stay in the heap pages. The
preceding `UPDATE` likewise leaves the old row versions on disk as dead tuples under MVCC.
Without a table rewrite the PANs and CVVs are still physically present and recoverable from
the data files, and the migration would be a documentation change rather than a remediation.
Where downtime is unacceptable, `pg_repack` achieves the same rewrite online.

**Out of this migration's reach, and stated so it is scheduled rather than assumed:** WAL
segments, replicas, and every backup taken before the rewrite still contain the data. Those
need their own retention/rotation action (scoping Q2).

**(b) What may be retained instead.**

```sql
ALTER TABLE payments
  ADD COLUMN IF NOT EXISTS card_token     TEXT,
  ADD COLUMN IF NOT EXISTS card_brand     TEXT,
  ADD COLUMN IF NOT EXISTS card_last4     CHAR(4),
  ADD COLUMN IF NOT EXISTS card_exp_month SMALLINT,
  ADD COLUMN IF NOT EXISTS card_exp_year  SMALLINT,
  ADD COLUMN IF NOT EXISTS bank_token     TEXT;
```

Token, brand, last-4, expiry. Nothing else. This answers ADR 0003's two real requirements —
a CSR verifying a caller against the card on file, and finance re-running a charge — without
retaining the instrument. The CVV is not used in card-on-file transactions at all, which is
why retaining it purchased nothing even on ADR 0003's own terms.

**(c) The PAN never reaches a Meridian server.** The browser collects it in
processor-hosted fields and exchanges it for a token client-side; only the token crosses
into the backend. ACH account and routing numbers are tokenized by the same mechanism —
they are not cardholder data, but there is no reason to hold them either.

**(d) The API refuses cardholder data.** `PaymentIn` drops `pan` and `cvv` and sets
`model_config = ConfigDict(extra="forbid")`. Removing the fields is insufficient on its own:
Pydantic silently ignores unknown fields by default, so a caller still sending `pan` would
get a `201` and a false sense that it was stored safely. **Fail closed** — reject the
request.

**(e) A blocking CI gate.** `no-sad-gate`, in the same family as `redactor-drift` and
`secret-scan` (no `continue-on-error`): asserts that `payments` has no `pan` or `cvv`
column in either `db/init/001_schema.sql` or the migration chain, that no service references
those columns, that the API schema forbids extras, and that no seed file contains a PAN or
CVV literal. Storage regressions must be caught the way redaction regressions already are.

**(f) Seed and docstring cleanup.** Scrub `db/init/002_seed.sql:68-74` and `:79` and
`003_seed_bulk.sql:74`. Correct the stale docstrings at `payment-service/app/payments.py:1-9`
and `app/main.py:1-7` that still claim the CVV is logged.

### D5. Payment state model (both rails)

| Rail | States | Balance applies at |
|---|---|---|
| Card | `processing` → `captured` \| `failed` | `captured` |
| ACH | `processing` → `submitted` → `settled` \| `returned` \| `failed` | **`settled`** |

The substantive change is that ACH `submitted` moves no money. Today `charge()` reports
`captured` and reduces the balance the moment the request succeeds, which is wrong for every
ACH payment that later returns — and Meridian learns about it weeks afterwards, from a
borrower.

`returned` exists in the model although return *handling* is out of scope. The state has to
be there now; adding it later means re-modelling every row.

Borrower-facing copy follows the model: an ACH payment reads "payment submitted", not
"payment received" (scoping Q4).

### D6. Self-serve access — the `pay:loan:{id}` capability token

Extends the ADR 0010 Phase B pattern. `db/migrations/0015_payment_capability_token.sql`:

```sql
ALTER TABLE loans
  ADD COLUMN IF NOT EXISTS payment_token            TEXT,
  ADD COLUMN IF NOT EXISTS payment_token_expires_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS payment_token_version    INTEGER NOT NULL DEFAULT 0;
```

**Inherited from `0008`/`0009` without redesign:** stored as a keyed hash only
(`authz.hash_token`), rejected when the expiry is `NULL` or past, and never exposed to the
browser — the gateway exchanges the raw token for an opaque Redis session id delivered in an
HttpOnly, SameSite=Strict, path-scoped cookie (`gateway/app/main.py:48-50`,
`auth.py:create_resume_session`) and re-attaches it inward as a header. localStorage was
already rejected on this codebase as XSS-reachable; that decision carries over.

**Scope.** One loan, one verb. The token authorizes `POST /payments` for its own `loan_id`
and a read of that loan's payoff figure. It authorizes nothing else — not the portfolio, not
another loan, not `adjust-balance`. Ownership is an invariant established at mint time by
the system that already knows who owns the loan, not a lookup performed per request.

**Design rules from scoping §3.3d, each answering a specific failure:**

1. **`GET` is non-consuming and side-effect-free.** Corporate mail scanners fetch every
   emailed URL before the recipient sees it; a token spent on `GET` is dead on arrival and
   the failure presents as an outage. Only `POST /payments` spends anything.
2. **One knowledge factor at the landing page** — date of birth, ZIP, or last-4 — before the
   payoff figure is displayed or a payment is accepted. The link alone is a bearer
   credential and forwarded mail is routine; the landing page shows financial data.
3. **`payment_token_version` enables bulk revocation.** Bumping it invalidates every
   outstanding link for that loan, and it is bumpable across a whole statement batch.
4. **`Referrer-Policy: no-referrer`** on the landing page.
5. **Lifetime** = one statement cycle, `PAYMENT_TOKEN_TTL_DAYS` (default 30), following the
   `CONTINUATION_TOKEN_TTL_DAYS` precedent. An expired link lands on a page offering the
   phone path, not an error.

**Token and idempotency key are separate concerns and must not be merged.** The token
answers *may this caller pay this loan* and survives multiple attempts, so a declined card
does not kill the link. The key answers *is this the same payment intent* and expires in
hours. Using a single-use token as the dedupe mechanism collapses the two and breaks on the
first declined card.

**Accepted risk, no mitigation:** the model trains borrowers that a mailed link opening a
payment page without a login is normal — the phishing pattern. Recorded in ADR 0013 as an
accepted risk whose answer is phase 3 (enrollment at sanction), not a control.

**Phasing** (scoping §3.3c): phase 1 (staff-assisted, existing logins) ships with D1–D5 and
needs none of D6. Phase 2 is D6 and depends on a verified delivery channel (Q7). Phase 3
(enrollment at sanction) is out of scope and requires Argon2 password hashing plus ADR 0010
RBAC.

### D7. ADR 0013

`adr/0013-payment-idempotency-and-tokenization.md`. Nygard format per the repo's ADR
standards. Records: the idempotency contract and why the constraint rather than application
logic is the enforcement point; tokenization and the no-SAD rule; the capability-token
access model and its accepted phishing risk; the deferral of RBAC and the ledger.
**Supersedes ADR 0003**, and must state how each of ADR 0003's stated business needs is met
rather than dropped. Minimum three options compared per option area, with rejection
reasons.

### D8. Live reproduction

`scripts/repro_double_charge.py` — drives the real `POST /payments` path through the gateway
and reports rows created, amount captured, and amount credited for a single payment intent,
in both the sequential-retry and concurrent cases. **Written and run; both defects
confirmed.**

It exists because the client's position is that the tickets are confusion, and a document
does not settle that. It also earns its keep as verification: it is the only artifact this
week that could have falsified the spec's premise, and it found the lost-update defect
(D3d) that the code read alone did not.

Operationally: cleanup is on by default and removes only the ids the run created, restoring
the opening balance. It requires `PROCESSOR_API_KEY` to be set, or payment-service fails
closed at `/health` and refuses every charge — which is the guard working as intended, since
no processor is ever actually contacted (the only outbound call in `charge()` is the
servicing apply hop, `payment-service/app/payments.py:96`).

---

## Out of Scope (Not This Week)

- **All implementation.** This week produces documents and executable test vectors only.
- **RBAC on servicing** — borrower-owns-the-loan on reads, officer-only on `adjust-balance`
  and `waive-fee`. Stays D8 / ADR 0010. D6 makes the payment-path half largely moot.
- **Phase 3 enrollment at sanction**, and the Argon2 password-hashing migration it requires.
- **The ledger** (D2). Constrained only: D3(b) is event-shaped and adds no mutable money
  column.
- **ACH return handling, settlement reconciliation, NACHA file format.** `returned` exists
  in the model; nothing drives it.
- **Consolidating the two charge handlers.** The unique index covers the correctness risk;
  the duplication is logged as new debt.
- **Converting existing float money columns.** New columns are minor units; old ones are not
  touched.
- **Re-tokenizing historical payments.** The CVV/PAN purge in D4(a) is in scope. Recovering
  tokens for historical cards is a separate migration with customer-communication
  implications.
- **Backup, WAL, and replica remediation** for cardholder data already written. Named in
  D4(a); owned outside this spec.
- **Procuring the delivery channel** (Q7).
- **Autopay and recurring payments.** The stored token and the event-shaped record are the
  seam; nothing is built.

---

## Acceptance Criteria (End of Week)

Criteria 1–15 are what the *implementation* must satisfy; they are the contract this spec
hands to the build week. Criteria 16–21 are what this week itself must deliver.

### Functional

1. A retried `POST /payments` with the same key and payload produces exactly one `payments`
   row, exactly one processor call, and a byte-identical response body.
2. Two concurrent requests with the same key produce exactly one row and exactly one
   processor call; the loser receives `409`, never a second charge.
3. A reused key with a different payload is refused (`422`) with no row and no processor
   call.
4. A missing or malformed key is refused (`400`) with no row and no processor call.
5. Applying the same `payment_id` twice moves the balance once and leaves exactly one
   `payment_applications` row. The application credits only the loan and the amount recorded
   on that `payments` row, so a `payment_id` presented against a different loan, a payment
   whose status does not credit, and a loan with no `balances` row are each refused and leave
   no application row behind.
6. **N distinct payments applied concurrently move the balance by exactly their sum.** No
   lost updates. The application record and the balance movement commit together or not at
   all.
7. A failed apply is never reported as a plain success; the response distinguishes captured
   from applied, and the reaper resolves the row.
8. An ACH `submitted` payment moves no balance; `settled` moves it exactly once.

### Security / Compliance

9. `payments` has no `pan` and no `cvv` column, in `db/init/001_schema.sql` and in the
   migration chain, and no service references either.
10. The purge migration rewrites the table (`VACUUM FULL` or `pg_repack`), so the values are
    not merely marked dropped.
11. A request carrying a `pan` or `cvv` field is rejected; the schema forbids unknown fields.
12. No seed or fixture file contains a PAN or CVV literal.
13. `apply-payment` without `X-Internal-Service` is refused and the balance is unchanged.
14. A capability token authorizes only its own loan and only the payment verb; an expired,
    version-superseded, or wrong-loan token is refused. `GET` never consumes it.
15. Existing blocking CI gates stay green, and `no-sad-gate` joins them.

### Process

16. All work on `feature/payments-week5` off `main`.
17. ADR 0013 committed, superseding ADR 0003, with 3+ options per decision area and
    rejection reasons.
18. Test vectors specified below, with the concurrency and duplicate-charge ones **measured
    red against `main`** rather than asserted. `scripts/repro_double_charge.py` is that
    measurement, committed and runnable: R1, R2, and A6 are demonstrated red by a live run.
    Committing them as executable tests — together with A1, A3, and T3 — is the build week's
    first task and is **not** delivered on this branch. A vector that cannot fail proves
    nothing, so each is written and watched fail before the fix it covers exists.
19. `docs/scoping-payments-week5.md` and this spec cite `main` for every claim about current
    state, with `file:line`, and the reproduction is committed and runnable.
20. `docs/debt-log.md` updated: D19 and D13 addressed-by-design; D8 partly addressed
    (internal gate) with the RBAC half still open; D2 partly addressed (atomic mutation)
    with the ledger and float columns still open; new entry for the two duplicate charge
    handlers.
21. `docs/kb.md` and the feature status tracker updated (tracker stays local — never pushed).

---

## Test Vectors

Committed as executable tests. Each names the acceptance criterion it proves. Money in minor
units.

### Retry and concurrency

| # | Scenario | Expected |
|---|---|---|
| R1 | `POST` key `K`, `loan 4471`, `25000`; then `POST` again, same key, same body | 1 `payments` row; 1 processor call; second response identical to first plus `Idempotent-Replay: true` |
| R2 | Two simultaneous `POST`s, key `K`, same body, **separate connections** | 1 row; 1 processor call; one `201`, one `409` with `Retry-After` |
| R3 | `POST` key `K` amount `25000`; then key `K` amount `50000` | `422`; still 1 row; still 1 processor call; balance moved once |
| R4 | `POST` with no `Idempotency-Key` | `400`; 0 rows; 0 processor calls |
| R5 | `POST` with `Idempotency-Key: not-a-uuid` | `400`; 0 rows; 0 processor calls |
| R6 | `POST` key `K`; age its `idempotency_expires_at` past `now()`; `POST` key `K` again | 2 rows; 2 processor calls **carrying two *different* `processor_idempotency_key` values**; treated as distinct payments. The first row survives with `idempotency_key` `NULL` and its `payment_applications` row intact — the key is retired, the payment is not |
| R6d | `POST` key `K`; retire its Meridian key (R6); `POST` key `K` again, against a **processor stub that still remembers** the first row's `processor_idempotency_key` and would collapse a reuse of it | 2 processor charges, because the second row's `processor_idempotency_key` differs from the first's. The stub's memory of the old value is never hit. Proves Meridian-key retirement does not couple to processor retention — the version-skew money bug cannot occur. Red on a spec that forwards the client key verbatim |
| R6b | Key `K` expired; **N** simultaneous `POST`s with `K` | 1 new row; 1 processor call; the rest get D1's concurrent answer. Retirement must not open a window where two requests both own the key |
| R6c | ACH key `K` still `submitted`, `idempotency_expires_at` aged past `now()`; `POST` key `K` again | `409` + `Retry-After`; **no** new row; **no** processor call; `K` still on the original row. An unfinished intent does not release its key just because the window passed |
| R7 | `POST` key `K` while an earlier `K` is still `processing` | `409` + `Retry-After`; no second processor call |
| R8 | Row left `processing` past the timeout; reaper runs | Resolved from the processor by the row's `processor_idempotency_key`; terminal status; exactly one application row |
| R9 | Reaper runs over an expired key on a terminal row and an expired key on a `submitted` row | Terminal row's key retired to `NULL`, its status and application row unchanged, a subsequent `POST` with that key inserts; the `submitted` row **keeps** its key, so the stuck-row path can still resolve it by that value |

R2 must run against real concurrency — two connections issued in parallel. A sequential
approximation passes trivially and proves nothing.

R6 ages the timestamp rather than sleeping: the window is a column, so the test controls it
directly and does not wait 24 hours. R6b and R6c are the vectors that keep R6 honest, one on
each side. R6b separates "expired keys become reusable" from "the unique index is now
advisory", and must run against real concurrency for the same reason R2 does. R6c is the
opposite failure — retirement reaching an intent that has not finished, which would both
re-open the duplicate charge and strip the value the reaper resolves the row by.

R6d closes the processor/version-skew vector: it drives the second insert against a processor
stub whose idempotency memory outlives Meridian's window and asserts the second charge is *not*
suppressed, which holds only because the value forwarded to the processor is the row-bound
`processor_idempotency_key`, not the reused Meridian key. Run it with the stub configured to
collapse on the *first row's* processor key so a regression that forwards the client key turns
it red.

### Migration / DDL

| # | Scenario | Expected |
|---|---|---|
| R-DDL | Apply `0012_payments_idempotency.sql` to a real Postgres, then run the **exact** claim insert from D2: `INSERT INTO payments (...) VALUES (...) ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING RETURNING id` | Both statements succeed; a first insert of key `K` returns a row, a second insert of `K` returns zero rows (key claimed). Proves the partial-index arbiter is inferable by the documented conflict target — a bare `ON CONFLICT (idempotency_key)` raises `no unique or exclusion constraint matching the ON CONFLICT specification` and fails this vector |

R-DDL runs the literal SQL string the implementation ships, against a live Postgres, not a
stub — the whole class of defect it guards is one Postgres rejects at plan time and no
`db.query`-mocking service test can see.

### Cross-service apply

| # | Scenario | Expected |
|---|---|---|
| A1 | `apply-payment` twice, same `payment_id` | 1 `payment_applications` row; balance moved once |
| A2 | Servicing unreachable during apply | Payment terminal; **no** application row; response does not claim applied; reaper retries to exactly one row |
| A3 | `POST /lss/accounts/1/apply-payment` with no `X-Internal-Service`, authenticated session | `403`; balance unchanged; no application row |
| A4 | Same, with a client-supplied `X-Internal-Service` through the gateway | `403` — the gateway does not forward client trust headers |
| A5 | `UPDATE` or `DELETE` on `payment_applications` | Refused by the append-only trigger |
| A6 | **Lost update** — N distinct payments applied concurrently to one loan | Balance moves by exactly the sum; N application rows. Red on `main`: an 8-way run credits $600 of $800 captured. |
| A7 | Application insert succeeds, balance update fails | Transaction rolls back; neither the row nor the movement persists |
| A8 | `POST /accounts/{loan B}/apply-payment` with a `payment_id` belonging to loan A | Refused; no application row; **neither** loan's balance moves |
| A9 | `apply-payment` for a loan with no `balances` row | Refused; transaction rolls back; no application row, so the retry after the balance row exists still applies |
| A10 | `apply-payment` for a payment whose status is `processing`, `submitted`, `failed`, or `returned` | Refused; no application row; balance unchanged |
| A11 | `apply-payment` body carrying an `amount` field | Rejected as an unknown field; no application row; balance unchanged |
| A12 | `POST /payments` against **servicing** directly (the second charge handler), which applies in-process | Balance moves and leaves exactly one `payment_applications` row, same as the endpoint path |

A8–A11 are the D3d derivation vectors: A8 and A11 prove the caller cannot choose the loan or
the amount, A9 proves a missing `balances` row does not leave a permanent application row that
`UNIQUE (payment_id)` would then block the retry on, A10 proves the crediting statuses. A12 is
D3e — the in-process apply path, which is the one a fix applied only at the endpoint misses.

A6 is the vector for D3d and is the one the reproduction found. It is distinct from R2:
R2 sends **one** intent N times (idempotency), A6 sends **N** genuine payments concurrently
(atomicity). Fixing either alone leaves the other red.

### Tokenization and PCI

| # | Scenario | Expected |
|---|---|---|
| T1 | `POST /payments` with a `pan` field | `400`; 0 rows; 0 processor calls |
| T2 | `POST /payments` with a `cvv` field | `400`; 0 rows; 0 processor calls |
| T3 | Schema assertion over `001_schema.sql` + migrations | No `pan`, no `cvv` column on `payments` |
| T4 | Source scan across all 7 services | No reference to `payments.pan` or `payments.cvv` |
| T5 | Seed scan (`002_seed.sql`, `003_seed_bulk.sql`) | No PAN or CVV literal |
| T6 | Post-migration heap inspection on a populated pre-migration volume | No PAN byte sequence recoverable after the rewrite |
| T7 | Successful card payment | Row holds `card_token`, `card_brand`, `card_last4`, expiry — and nothing else about the instrument |
| T8 | Log assertion across every vector above | No PAN or CVV shape in any log line |

T6 is the vector that distinguishes a real purge from a schema change, and it requires a
populated pre-migration volume — the Week 4 lesson that a suite run from a repo checkout
cannot see what a live volume does.

### Capability token

| # | Scenario | Expected |
|---|---|---|
| C1 | Valid token for loan 4471, `POST /payments` loan 4471 | Accepted |
| C2 | Valid token for loan 4471, `POST /payments` loan 4472 | `403` |
| C3 | Expired token | `403` |
| C4 | Token whose `payment_token_version` was superseded | `403` |
| C5 | `GET` the landing page 5 times, then `POST` | All `GET`s succeed and consume nothing; the `POST` succeeds |
| C6 | `POST` without the knowledge factor | `403`; payoff figure never rendered |
| C7 | Token presented against `adjust-balance` or the portfolio read | `403` |
| C8 | Raw token searched for in the DB after issuance | Only a keyed hash present |

### ACH

| # | Scenario | Expected |
|---|---|---|
| H1 | ACH payment reaches `submitted` | Balance unchanged; no application row |
| H2 | Same payment transitions to `settled` | Balance moves once; 1 application row |
| H3 | Same payment transitions to `returned` after settling | Modelled; no automatic reversal this week (documented gap) |
| H4 | ACH response body and borrower copy | Says submitted, never received or captured |

---

## Verification

- **Blocking CI:** `no-sad-gate` (D4e). Idempotency and apply-dedupe vectors run outside the
  tolerated `|| true` backend matrix — a duplicate charge is a money defect, and the Week 4
  precedent (`tila-vectors-gate`) exists because a money test under `|| true` let a
  regulatory defect ship.
- **`make prove`** for every regression test the build week commits: the source is rolled back
  to the parent commit and the test must FAIL without the fix and PASS with it. Acceptance
  criterion 18 names R1, R2, A1, A3, A6, and T3 as the minimum to be demonstrated red on
  `main` first; R1, R2, and A6 already are, by live run of the reproduction.
- **Live stack:** the migration chain applied to a **populated** pre-migration volume, not a
  fresh one — T6, the append-only trigger, and the partial unique index against legacy NULL
  keys are all only observable there.
- **Concurrency harness:** R2, A1, and A6 driven from parallel connections against `make up`.
  `scripts/repro_double_charge.py` is that harness for the pre-fix side and has already been
  run: R1 red (2 rows for one intent), R2 red (8 rows for one intent), A6 red ($800 captured
  against $600 credited). The build week's job is to turn the same script green.

---

## Client Questions

Carried from `docs/scoping-payments-week5.md` §6. None block the spec; Q7 changes the launch
sequence rather than the design. Assumptions are recorded there so a different answer costs
a configuration change.

Q1 processor and hosted fields · Q2 historical card data ownership · Q3 card-on-file
requirement · Q4 ACH borrower copy · Q5 duplicate window · Q6 remediation for the three
existing tickets · Q7 delivery channel · Q8 link lifetime.

---

## Status

Spec complete. ADR 0013 committed. The executable test vectors are the build week's first
task (criterion 18); the red evidence this week rests on `scripts/repro_double_charge.py`,
which has been run live.
