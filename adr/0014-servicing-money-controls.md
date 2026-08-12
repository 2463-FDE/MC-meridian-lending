# ADR 0014: Servicing Money Controls — Authorization, Maker-Checker, and a Balance Ledger

- **Status:** Proposed
- **Date:** 2026-08-12
- **Deciders:** Engineering, with Lending Ops (Dana Whitfield, VP Lending Ops) as requesting
  stakeholder
- **Related:** ADR 0010 (application ownership authorization — its deferred servicing half),
  ADR 0013 (payment idempotency; decides the atomic apply and `payment_applications`),
  ADR 0012 (Decimal/minor units), ADR 0009 / 0008 (`decision_events`, the append-only
  pattern this reuses), debt **D8**, **D3**, **D2**, **D20**
- **Scope note:** This ADR decides. It ships no code, no schema, and no migration. SQL below
  is illustrative of the shape, not a migration file.

---

## Context

Lending Ops asks for a servicing dashboard so representatives can correct balances and waive
fees without an engineer running SQL. The request is reasonable and the underlying capability
already exists — as four unguarded endpoints.

**Any authenticated caller can move money on any loan.** `servicing-service` has no
authorization module. `adjust_balance` and `waive_fee` declare `x_user_role`
(`services/servicing-service/app/main.py:103,114`) and never read it; `balance.adjust_balance`
and `balance.waive_fee` (`app/balance.py:35-56`) take no caller identity at all. The gateway
authenticates the session and forwards authoritative `X-User-Id` / `X-User-Role` after
stripping any client copy (`services/gateway/app/main.py:160-171`), but `/lss/*` and
`/payments` gate on `_require_user` only (`gateway/app/main.py:193-197`). Borrower `maria` can
zero a stranger's balance. Loan ids are serial, so reads
(`services/servicing-service/app/routers/loans.py:61-77`, `main.py:88-94`) enumerate every
customer. This is debt **D8**, Critical, and ADR 0010 deferred it for servicing on the grounds
that no identity was bound to the resource.

**No approver exists for a discretionary move.** `adjust-balance` sets the balance to an
operator-supplied number; the prior value is not recorded anywhere
(`app/balance.py:35-43`). One representative acting alone, or one mistyped field, is the whole
control path.

**Balance history is unreconstructable.** `balances` is one mutable `DOUBLE PRECISION` column
with an overwritten `updated_at` (`db/init/001_schema.sql:117-122`). No actor, delta, prior
value, or reason is stored. `audit_logs` is an ordinary mutable table with a `deleted_at`
column (`001_schema.sql:137-144`), no servicing code writes it, and its rows are
`UPDATE`/`DELETE`-able — debt **D20**. So "why is account 7781 at this number?" is answerable
today only as "that is the number", plus a timestamp. The one genuinely append-only table in
this schema is `decision_events` (`001_schema.sql:148-179`): serial primary key, JSONB
payload, a `BEFORE UPDATE OR DELETE` trigger and a `BEFORE TRUNCATE` trigger that both raise.

**What is already decided elsewhere.** The concurrency defect on this surface is not open for
decision here. `balance.apply_payment` is a read-modify-write across two autocommit
round-trips (`app/balance.py:23-32`; `app/db.py:9-14` sets `autocommit = True`), measured on
2026-08-02 at $800.00 captured against $600.00 credited over eight concurrent applies — debt
**D3**. ADR 0013 decides the fix: one atomic `UPDATE balances SET balance = balance - :amount`
committed with an append-only `payment_applications` row unique on `payment_id`. That fix is
specified and not built; migrations on `main` stop at
`db/migrations/0016_provenance_disclosure_outcome.sql`. This ADR builds on 0013 and does not
restate it.

The gap this ADR closes is therefore narrower than the debt log's headline: **who may move
money, who must approve a discretionary move, and where the movement is recorded** — for the
manual paths ADR 0013 does not touch.

---

## Decision

### Decision 1 — Servicing authorizes with the ADR 0010 module shape, copied into the service

We will add `services/servicing-service/app/authz.py` mirroring
`services/origination-service/app/authz.py`: an `_OFFICER_ROLES = {"underwriter", "admin"}`
set, a `require_officer` that raises 403, and a `require_officer_or_owner` that denies as 404
rather than 403-on-exists so a serial id cannot be probed for existence.

Applied per route:

| Route | Gate |
|---|---|
| `POST /accounts/{id}/adjust-balance` | officer, and maker-checker (Decision 2) |
| `POST /accounts/{id}/waive-fee` | officer, and maker-checker (Decision 2) |
| `POST /accounts/{id}/apply-payment` | internal-service only, per ADR 0013 |
| `POST /accounts/{id}/late-fee` | internal-service only — rule-driven, no operator |
| `GET /accounts/{id}/balance`, `GET /loans/{id}`, `/loans/{id}/payments`, `/loans/{id}/schedule` | officer or owner |

Ownership needs no new column and no identity programme, which is what ADR 0010 deferred on.
It derives from data that exists: `loans.app_id` (`001_schema.sql:99-114`) reaches
`applications.applicant_id`, and a borrower login carries `users.applicant_id`
(`001_schema.sql:6-15`). A loan whose `app_id` is `NULL` — legacy rows the partial unique
index deliberately tolerates — has no derivable owner, so it fails closed to officers.

**Options considered**

| Option | Why rejected |
|---|---|
| **A. Enforce roles in the gateway only** | The gateway is the wrong enforcement point for ownership: it would need the loan-to-applicant join, duplicating servicing's data access, and it leaves the service open to any in-cluster caller. CLAUDE.md records the gateway's role-free proxying as intentional; a partial exception for `/lss/*` is a second contradictory model. |
| **B. Extract a shared authz package for both services** | Rejected as premature under this repo's YAGNI rule: two callers, not three. The per-service copy is this codebase's existing shape (the PII redactor is duplicated per service and held by the blocking `redactor-drift` job). If a third service needs it, extract then. |
| **C. Accept the risk, gate in the dashboard UI** | Rejected. The endpoints stay reachable directly through the gateway, so the control lives where it can be skipped. |
| **D. Chosen: copy the ADR 0010 module shape into servicing** | Reuses a reviewed pattern, needs no migration, and closes both halves of D8(b) — role on mutations, ownership on reads. |

The accepted cost is a second copy that can drift from origination's. It is logged as debt and
becomes the third repetition that would justify extraction.

### Decision 2 — Maker-checker binds the two manual discretionary moves only

We will require a second approver for `adjust-balance` and `waive-fee`, and for nothing else.
A maker (role `csr`, `underwriter`, or `admin`) records a request; a checker
(`underwriter` or `admin`) approves it; the balance moves on approval, not on request. The
checker cannot be the maker — compared on `users.id`, so one person holding two roles cannot
self-approve. Approval and movement commit together.

`apply-payment` and `late-fee` are excluded on a stated principle: **maker-checker binds
discretion, not automation.** `apply-payment` credits an amount read off a captured payment
row (ADR 0013), and `late-fee` applies a rule. Neither has an operator-chosen amount for a
second human to check, and both are called by another service, so a pending-approval state
would stall a payment mid-flow.

**Options considered**

| Option | Why rejected |
|---|---|
| **A. Every money-affecting endpoint** | Rejected. It puts a human approval in the middle of `apply-payment`, which is on the borrower's payment path and is invoked by `payment-service` — money would be captured and then wait. It also gold-plates two routes that have no discretionary input. |
| **B. Threshold-based — second approval over a dollar amount** | Rejected for now. It needs a threshold nobody has set, a policy file to hold it, and a currency comparison in the gate, and it leaves small adjustments uncontrolled — which is the shape of the loss this control exists to prevent. The ledger (Decision 3) makes every move attributable regardless of size, so the threshold buys less than it costs. It is the natural later refinement once the two-approver flow is in use. |
| **C. Chosen: manual discretionary moves only** | Smallest control that covers the operator-chosen amounts, with no effect on automated paths. |

The accepted cost is operational: two staff are needed to correct a balance, and a
single-officer shift cannot. Lending Ops confirms the approver set before build.

### Decision 3 — Every balance mutation writes a row to one append-only ledger

We will add `balance_postings`, append-only by the `decision_events` trigger pattern, and make
the balance a projection of it rather than an independently mutable number:

```sql
-- Illustrative shape, not a migration.
CREATE TABLE balance_postings (
    id            SERIAL PRIMARY KEY,
    loan_id       INTEGER NOT NULL REFERENCES loans(id),
    column_name   TEXT NOT NULL,        -- 'balance' | 'past_due'
    delta_minor   BIGINT NOT NULL,      -- signed, integer minor units (Decision 4)
    reason_code   TEXT NOT NULL,
    detail        JSONB NOT NULL,       -- identifier-free, per ADR 0007
    made_by       INTEGER REFERENCES users(id),
    approved_by   INTEGER REFERENCES users(id),
    payment_id    INTEGER REFERENCES payments(id),
    posted_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- BEFORE UPDATE OR DELETE and BEFORE TRUNCATE triggers raising, per decision_events.
```

`balances.balance` remains as a cached projection so read paths and the reconciliation totals
keep working, and `SUM(delta_minor)` over the ledger is the authority. Any posting and the
`UPDATE balances` it authorizes commit in one transaction, so a recorded movement that did not
happen — and a movement with no record — are both unrepresentable.

**On the seam with ADR 0013.** ADR 0013 calls `payment_applications` "the ledger seam: a
future ledger reads this table rather than re-modelling". This is that ledger, and the two do
not compete: `payment_applications` is the idempotency record, unique on `payment_id`, and the
posting is the movement. Whichever lands first, the payment apply path writes both rows in its
existing single transaction, and `payment_id` on the posting is the join. If ADR 0013's
migration ships first, the payment path gains a posting insert; if this one ships first,
0013's step 2 includes it.

**Options considered**

| Option | Why rejected |
|---|---|
| **A. Keep the mutable column, add an audit trigger on `balances`** | Rejected: this is exactly D20 again. `audit_logs` already demonstrates a trail nobody trusts — it is mutable, soft-deletable, and unpopulated. A trigger-written history table that can itself be edited is not evidence, and a trigger also cannot see the actor or the reason, which is what a reconstruction needs. |
| **B. Full event sourcing — drop the column, recompute the balance on every read** | Rejected. It rewrites every servicing read path, the schedule, delinquency, and the reconciliation totals for a book that has no scale problem, and it turns a single-row read into an aggregate on the hottest query. The projection gives the same authority with a bounded change. |
| **C. Extend `payment_applications` to cover manual moves** | Rejected. Its `UNIQUE (payment_id)` is its whole purpose; a manual adjustment has no payment, so the column would go nullable and the constraint would stop binding the case it was added for. Two different jobs, two tables. |
| **D. Chosen: one append-only ledger plus a projection** | Reuses a pattern already enforced in this schema, keeps read paths intact, and makes actor, delta, prior value, and reason recoverable for every mutation. |

### Decision 4 — The ledger is integer minor units; `balances` stays float for now

We will store `delta_minor` as `BIGINT` minor units, per ADR 0012's precedent and ADR 0013's
rule that new money columns use minor units while existing float columns are left alone. The
ledger is the integer of record; the `balances` projection stays `DOUBLE PRECISION` and is
converted on write.

**Options considered**

| Option | Why rejected |
|---|---|
| **A. Convert `balances`, `payments`, and `loans` to minor units in this change** | Rejected as scope. It touches every servicing read path, the schedule, delinquency, and reconciliation, and it would land in the same change as a new authorization model and a new control — a diff nobody can review. D2's remaining columns get their own migration. |
| **B. Keep the ledger in float too, for consistency with `balances`** | Rejected. A ledger whose sum is the authority cannot be float: the rounding error compounds per posting, and the projection would disagree with its own source. That is the defect the ledger exists to make visible. |
| **C. Chosen: minor units in the ledger, float projection** | The authoritative number is exact; the conversion is one place; D2 is narrowed on the same path ADR 0012 and ADR 0013 already narrowed it. |

The accepted cost is a mixed representation on one table pair, and a reconciliation that must
compare an exact sum to a float cache with an explicit tolerance.

---

## Consequences

### Positive

- A borrower can no longer move money on any account, and cannot read another customer's loan.
  D8(b) closes.
- A discretionary balance correction requires two people, and names both.
- "Why is account 7781 at this number?" becomes answerable: every delta carries actor,
  approver, reason, and time, in a table the database refuses to let anyone edit.
- The reconciliation totals gain a third number that must tie out, so a future divergence is
  attributable to a loan rather than only visible in aggregate.
- ADR 0010's servicing deferral ends without the identity programme it was waiting on.

### Negative / tradeoff (accepted)

- **Two people are needed to correct a balance.** A single-officer shift cannot, and a
  mistake takes longer to fix than it does today.
- **A second authz copy exists**, free to drift from origination's. Logged as debt; the
  redactor's `redactor-drift` job is the precedent for holding it if it becomes a third copy.
- **`balances` stays a mutable float column.** It is a projection now, but the float and the
  mutability both remain. D2 is narrowed again, not closed.
- **Reads become owner-scoped, which changes existing behaviour.** Any script or demo step
  calling `/lss/*` without an officer role stops working.
- **A `NULL`-`app_id` loan is officer-only forever**, because no owner is derivable. Correct,
  and invisible until someone asks why a borrower cannot see an old loan.
- **The dashboard is not built by this ADR.** Lending Ops' original ask is still outstanding
  after this work; what changes is that building it no longer ships an unguarded money route.

### Neutral

- One new table and two triggers, matching `decision_events`.
- Four routes gain a header-derived gate; two become internal-only, which ADR 0013 already
  requires for `apply-payment`.

---

## Cross-cutting concerns

**Security.** The gate fails closed: an absent or unrecognized role is not an officer, and a
non-owner is denied as 404 so ids cannot be probed. The trust boundary is unchanged — the
gateway strips inbound `X-User-Id` / `X-User-Role` and re-sets them from the session
(`gateway/app/main.py:160-171`), which is what makes the header trustworthy inside servicing;
the existing `gateway-trust-boundary-gate` holds that. Ledger `detail` is JSONB and must stay
identifier-free per ADR 0007, so a reason string cannot become a new PII store.

**Performance.** One extra insert per mutation and one authorization lookup per request. The
ownership join is two indexed reads by primary key. The projection keeps reads at one row.

**Scalability.** The ledger grows unbounded, one row per mutation — trivial at this book size,
and the same growth profile `decision_events` already accepts.

**Reliability.** Posting and projection commit together, so neither can outlive the other.
This is the first servicing write path that requires an explicit transaction, which is a real
change: `app/db.py:13` runs `autocommit = True`, so the transaction has to be opened
deliberately and the module-level shared connection reviewed for it.

**Maintainability.** Two authz copies, and a ledger the manual and payment paths both write.
The seam with ADR 0013 is stated above precisely because whichever lands second must not
re-model it.

**Cost.** Storage only.

**Operational impact.** Lending Ops needs an approver roster before build, and a documented
path for the case where an approval is genuinely urgent and one officer is available. The
runbook gains the reason-code list.

**Testing impact.** Characterization tests pin today's behaviour before any of this changes
(the companion comprehension PR). New coverage: role and ownership matrix per route, maker
equals checker refused, posting and projection agreeing after each mutation, the append-only
triggers refusing `UPDATE`, `DELETE`, and `TRUNCATE`. The trigger tests need real Postgres,
not SQLite. Expect a dedicated blocking CI job for the authorization matrix, on the model of
`adr-0010-authz-gate` and `kyc-enforcement-gate`.

---

## Implementation plan

Later PRs, in this order. Each is independently mergeable.

1. **Authorization** — `servicing-service/app/authz.py` plus the per-route gates from
   Decision 1, and the internal-service gate on `late-fee`. No schema. Closes D8(b).
2. **Ledger** — migration adding `balance_postings` and its two triggers, at the next free
   number after ADR 0013's unbuilt migrations land (0013's plan cites 0016–0019 and `main`
   already holds an unrelated `0016`, so the number is read at write time, not pinned here).
   Backfill is not attempted: the history does not exist to backfill, and the ledger starts at
   its first posting with that stated in the runbook.
3. **Posting on every mutation** — `balance.py` writes a posting and the projection in one
   transaction. Both `apply_payment` call sites must be covered: the HTTP route
   (`main.py:84`) and the in-process caller (`app/payments.py:79`).
4. **Maker-checker** — the request-and-approve flow for `adjust-balance` and `waive-fee`.
5. **Dashboard** — the original ask, now on top of gated routes and an audit trail.
6. **Deferred, own ADR:** converting the remaining float money columns (D2), and the
   `audit_logs` append-only fix (D20).

Steps 1–4 are the control. Step 1 alone removes the Critical finding, so it should not wait
for the ledger.

---

## Rollback strategy

Steps 1, 3, and 4 are code and revert cleanly; behaviour returns to today's, which is why
step 1 shipping alone is safe. Step 2 is additive: `balance_postings` can be dropped, or left
in place unwritten, with no effect on any existing read.

The append-only triggers are the exception, and deliberately so — once postings exist they
cannot be edited or removed row-wise, only by dropping the table. A correcting posting is the
supported reversal for a wrong entry, not an `UPDATE`. Same property as `decision_events`, and
the reason that table is trusted.

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Owner-scoped reads break the demo or a script that calls `/lss/*` with a borrower or unset role | Run the demo flow against the gated build before merge; the authorization matrix test enumerates every route and role, including unset |
| The projection drifts from `SUM(delta_minor)` — a write path bypasses the posting | Reconciliation compares the two with an explicit float tolerance and reports the gap; a bypass is a failing test, not a silent divergence |
| The transaction is wrong because servicing has never needed one — `db.py` is `autocommit = True` on a module-level shared connection | Treat the transactional write as the risky part of step 3: explicit transaction, reviewed against the shared-connection lifetime, with a concurrency test that a rolled-back posting leaves the projection untouched |
| Maker-checker is bypassed operationally — one person uses two accounts | Compare on `users.id`, record both ids on the posting, and make same-actor approval detectable in the ledger even if policy is broken |
| A representative cannot fix a balance quickly and falls back to raw SQL, which no gate sees | Postgres role separation is out of scope here; the mitigation is operational — the runbook names the approval path and reconciliation surfaces a direct write as a projection-versus-ledger gap |
| ADR 0013's migrations land in a different order and re-model the payment posting | The seam is stated in Decision 3; whichever ships second adds the missing insert to the existing transaction rather than a second table |
| The reason code becomes a free-text field carrying PII | Fixed code list plus identifier-free JSONB per ADR 0007; the existing redaction gates cover the log path |

---

## What could supersede this

Converting the remaining float money columns (D2) would make the projection exact and remove
the tolerance from reconciliation. A real identity and enrollment flow would replace the
derived-ownership join with a bound borrower identity, which is the end state ADR 0010 names.
Neither changes the decisions here; both narrow the accepted costs.
