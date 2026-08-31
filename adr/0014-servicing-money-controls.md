# ADR 0014: Servicing Money Controls — Authorization, a Balance Ledger, and Deferred Approval

- **Status:** Proposed — the four decisions below rest on business answers Lending Ops
  confirmed in writing on 2026-08-12, transcribed in `docs/client-asks/week6-servicing-answers.md`
  with the questions that produced them; the ADR itself is awaiting engineering review
- **Date:** 2026-08-12
- **Deciders:** Engineering, with Lending Ops (Dana Whitfield, VP Lending Ops) as requesting
  stakeholder
- **Related:** ADR 0010 (application ownership authorization — its deferred servicing half),
  ADR 0013 (payment idempotency; decides the atomic apply and `payment_applications`),
  ADR 0012 (Decimal/minor units), ADR 0009 / 0008 (`decision_events`, the append-only
  pattern this reuses), debt **D8**, **D3**, **D2**, **D20**
- **Scope note:** This ADR decides. It ships no code, no schema, and no migration. SQL below
  is illustrative of the shape, not a migration file.
- **On being a gate anchor while Proposed:** `scripts/spec_gate_map.txt` maps the four
  servicing files this ADR obligates to this document. `spec_diff_gate.sh` is an existence
  check keyed on "has this spec/ADR merged to main", not on ADR `Status`, so a merged-but-
  Proposed document is a valid anchor by the gate's own stated rule (see the map's header
  comment and the script's docstring) — the same pattern already holds for ADR 0012 and ADR
  0013, both merged while Proposed. Flip this line to Accepted when engineering review closes;
  the gate does not require it.

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
`/payments` gate on `_require_user` only (`services/gateway/app/main.py:193-197`). Borrower `maria` can
zero a stranger's balance. **Lending Ops confirms this is externally reachable**: the borrower
portal sits behind the same gateway as the internal application, so a borrower login is on the
public internet. That answer changes the risk class rather than the finding — the same defect
read as an internal-misuse problem while the endpoints were assumed to be reachable only from
inside, and reads as an internet-facing one now. It is why the client asked for the
authorization fix as its own immediate change rather than as part of the dashboard work. Loan ids are serial, so reads
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
today only as "that is the number", plus a timestamp. This is not hypothetical: `README.md`
names Sam as the client's SOX/reconciliation contact (repo-observed fact); no one asked Sam
whether a walkthrough assumes an adjustment trail exists, so that inference is engineering's,
not a client-stated answer — `docs/client-asks/week6-servicing-answers.md` does not carry it. The
retention answer Sam did give (Q7, seven years) is cited on its own terms below. The one
genuinely append-only table in
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

We will add `services/servicing-service/app/authz.py` mirroring the *shape* of
`services/origination-service/app/authz.py` — a role set, a raising `require_*` gate, and an
owner check that denies as 404 rather than 403-on-exists so a serial id cannot be probed for
existence — with role sets of its own:

```python
_MONEY_ROLES = {"csr", "admin"}                       # may act on a balance at all
_STAFF_ROLES = {"csr", "underwriter", "admin"}        # may read any serviced loan
```

**The role set is servicing's own, not origination's.** Origination's
`_OFFICER_ROLES = {"underwriter", "admin"}` is wrong here in both directions: a CSR is the
role that actually services accounts, and an underwriter decides applications rather than
adjusting live balances. Reusing that set would have excluded the operator this dashboard is
for. Reads stay broader than writes because an underwriter looking at a serviced loan is
ordinary work.

Applied per route:

| Route | Gate |
|---|---|
| `POST /accounts/{id}/adjust-balance` | `_MONEY_ROLES`; recorded posting (Decision 3); approval deferred (Decision 2) |
| `POST /accounts/{id}/waive-fee` | `_MONEY_ROLES`; recorded posting (Decision 3); approval deferred (Decision 2) |
| `POST /accounts/{id}/apply-payment` | internal-service only, per ADR 0013 |
| `POST /accounts/{id}/late-fee` | internal-service only — rule-driven, no operator |
| `GET /accounts/{id}/balance`, `GET /loans/{id}`, `/loans/{id}/payments`, `/loans/{id}/schedule` | `_STAFF_ROLES`, or the owning borrower |

Ownership needs no new column and no identity programme, which is what ADR 0010 deferred on.
It derives from data that exists: `loans.app_id` (`001_schema.sql:99-114`) reaches
`applications.applicant_id`, and a borrower login carries `users.applicant_id`
(`001_schema.sql:6-15`). A loan whose `app_id` is `NULL` — legacy rows the partial unique
index deliberately tolerates — has no derivable owner, so it fails closed to staff.

**Both sets are confirmed by Lending Ops** (2026-08-12, answering the week-6 client email):
CSRs and admins move money, underwriters and borrowers do not, borrowers read their own account
only, and **no supervisor role is to be invented** — none exists today and creating one is work
the client would rather spend elsewhere. That last point is why maker and checker draw from one
set when approval does arrive.

**Options considered**

| Option | Why rejected |
|---|---|
| **A. Enforce roles in the gateway only** | The gateway is the wrong enforcement point for ownership: it would need the loan-to-applicant join, duplicating servicing's data access, and it leaves the service open to any in-cluster caller. CLAUDE.md records the gateway's role-free proxying as intentional; a partial exception for `/lss/*` is a second contradictory model. |
| **B. Extract a shared authz package for both services** | Rejected as premature under this repo's YAGNI rule: two callers, not three. It is also the wrong shape here — the two services need *different* role sets, so a shared module would carry a per-service configuration seam to serve one caller each. The per-service copy is this codebase's existing shape (the PII redactor is duplicated per service and held by the blocking `redactor-drift` job). If a third service needs it, extract then. |
| **C. Accept the risk, gate in the dashboard UI** | Rejected. The endpoints stay reachable directly through the gateway, so the control lives where it can be skipped. |
| **D. Chosen: copy the ADR 0010 module shape into servicing, with servicing's own role sets** | Reuses a reviewed pattern, needs no migration, and closes both halves of D8(b) — role on mutations, ownership on reads. |

The accepted cost is a second copy that can drift from origination's. It is logged as debt and
becomes the third repetition that would justify extraction. The divergent role sets are the
substantive part of that risk: a reader who assumes servicing's `authz.py` is origination's
will assume the wrong set, so each set carries the reason it differs.

### Decision 2 — Record every discretionary move now; approve them next cycle

We will make every manual balance move a recorded fact immediately, and we will not gate it on
a second person in this cycle. Each `adjust-balance` and `waive-fee` writes the actor, the
figure before, the figure after, and a reason (Decision 3). Nothing waits for an approval,
because there is no approval state to wait in yet.

Lending Ops holds the approval workflow deliberately, with the numbers to support it: about
**30 balance adjustments and 15 fee waivers a week across 9 representatives, with 3 people who
could approve**. A mandatory second approver at that ratio slows the floor before anything has
been measured, and the pending state, the queue, and the break-glass path it needs are a build
of their own rather than a flag on this one.

**The approval design is decided even though it is deferred**, so the ledger does not have to
change shape to accept it later. When it is built: any other CSR or admin approves, never the
same person — compared on `users.id`, not on role, so holding two roles or two sessions does
not permit self-approval — and a break-glass move is permitted, recorded, and reviewed after
the fact rather than blocked. `balance_postings.approved_by` exists from the first migration and
stays `NULL` until then, so turning approval on adds a write path and a queue, not a schema
change or a backfill.

The scope ruling holds regardless of timing: **approval binds discretion, not automation.**
`apply-payment` credits an amount read off a captured payment row (ADR 0013) and `late-fee`
applies a rule; neither has an operator-chosen amount for a second human to check, and both are
called by another service, so a pending state there would strand a captured payment. A
representative reversing a late fee by hand — which happens — goes through `waive-fee` and gets
that path's record and reason. There is no separate manual late-fee flow.

**Options considered**

| Option | Why rejected |
|---|---|
| **A. Mandatory second approver now, on the two manual moves** | Rejected by Lending Ops on measured operational grounds: 45 discretionary moves a week against 3 available approvers would queue the floor, and the pending state, queue, and break-glass path are a build that displaces work already committed this cycle. It is not rejected on merit — it is the next cycle's card, with its design fixed here. |
| **B. Threshold-based — second approval over a dollar amount** | Rejected. A threshold invites splitting one adjustment into two under it, which is the client's own objection as well as ours, and it leaves small moves uncontrolled. There is a $150-per-account-per-month waiver guideline in the ops manual, but it is recorded and displayed rather than enforced (Decision 3), so it is guidance to a representative, not a gate. |
| **C. No control at all until the dashboard is built** | Rejected. The record is the part that cannot be reconstructed afterwards, so deferring it costs history that is gone for good. Approval can start late; recording cannot start late. |
| **D. Chosen: record now, approve next cycle, design fixed now** | Delivers the attributable trail this week, keeps the floor moving at its measured volume, and leaves the approval build additive rather than a redesign. |

The accepted cost is real and worth stating plainly: **for one cycle, a single representative
can still move a balance alone.** What changes is that the move is attributable, reversible by a
correcting posting, and reviewable after the fact. The control that prevents it rather than
recording it is scheduled, not designed away.

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
    before_minor  BIGINT NOT NULL,      -- figure before this posting
    after_minor   BIGINT NOT NULL,      -- figure after; before + delta, stored not derived
    entry_type    TEXT NOT NULL,        -- 'opening' | 'adjustment' | 'waiver' | 'payment' | 'late_fee'
    reason_code   TEXT NOT NULL,        -- ops-manual code, or 'other' with reason_text set
    reason_text   TEXT,                 -- required when reason_code = 'other'
    detail        JSONB NOT NULL,       -- identifier-free, per ADR 0007
    made_by       INTEGER REFERENCES users(id),
    approved_by   INTEGER REFERENCES users(id),   -- NULL until the approval workflow ships
    payment_id    INTEGER REFERENCES payments(id),
    posted_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- BEFORE UPDATE OR DELETE and BEFORE TRUNCATE triggers raising, per decision_events.
```

`before_minor` and `after_minor` are stored rather than derived. A sum can be recomputed, but
Lending Ops asked for the figure before and the figure after on the record itself, and that is
what a controller reads a row for — a reconstruction that depends on replaying every prior
posting is not the same artifact.

**The reason is required, and the code list is the client's.** Lending Ops sends the ops-manual
codes on Friday 2026-08-14; until they arrive, `reason_code = 'other'` with free-text
`reason_text` is the only value, and `'other'` remains permanently available as the fallback once the list exists. So the
column is a list plus an escape hatch, never a closed enum — a representative who cannot find
their case must not be forced into a wrong code to complete a correction.

**The ledger opens with a labelled opening posting.** No history exists to backfill and none can
be reconstructed, so at cutover each account gets one `entry_type = 'opening'` posting carrying
today's stored balance. It is labelled as the opening figure precisely because that figure may be
wrong — the D3 concurrency defect is the same one behind the three double-charge tickets. A
reconciliation against payment records before cutover is a separate project, carded rather than
done, on Lending Ops' explicit preference for an honest line in the sand over a reconstructed
history nobody can defend.

`balances.balance` remains as a cached projection so read paths and the reconciliation totals
keep working, and `SUM(delta_minor)` over the ledger is the authority. Any posting and the
`UPDATE balances` it authorizes commit in one transaction, so a recorded movement that did not
happen — and a movement with no record — are both unrepresentable.

**The $150 waiver guideline is recorded, not enforced.** The ops manual sets $150 per account per
month on fee waivers with escalation above it, and the system has never enforced it. This ADR
does not start: the dashboard shows the representative the limit and the month's total to date,
and the reason is captured either way. Enforcement is carded. Encoding an unenforced guideline as
a hard gate would change what representatives can do on the day the ledger ships, which is a
policy change disguised as a logging change.

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

This decision is ours alone. It was never put to Lending Ops, because how money is represented
in storage is not a business question — the client's interest is that the figures are right, and
the cross-cutting cost of getting it wrong (D2) is already documented. So unlike Decisions 1–3,
nothing here rests on a client answer, and nothing changes if one arrives.

We will store `delta_minor`, `before_minor`, and `after_minor` as `BIGINT` minor units, per
ADR 0012's precedent and ADR 0013's rule that new money columns use minor units while existing
float columns are left alone. The ledger is the integer of record; the `balances` projection
stays `DOUBLE PRECISION` and is converted on write. The opening posting is the one place a
float becomes the integer of record, and it is labelled as such.

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
- Every discretionary balance correction names the person who made it, from the day the ledger
  ships.
- "Why is account 7781 at this number?" becomes answerable from cutover forward: every posting
  carries the actor, the figure before, the figure after, the reason, and the time, in a table
  the database refuses to let anyone edit.
- Lending Ops can answer Sam's SOX walkthrough with a date rather than a gap: history starts at
  cutover, and nothing before it is claimed.
- The reconciliation totals gain a third number that must tie out, so a future divergence is
  attributable to a loan rather than only visible in aggregate.
- ADR 0010's servicing deferral ends without the identity programme it was waiting on.

### Negative / tradeoff (accepted)

- **For one cycle, a single representative can still move a balance alone.** Approval is
  deferred by the client's decision, so this cycle buys attribution rather than prevention. The
  posting makes the move visible and a correcting posting reverses it, but nothing stops it.
- **The $150 waiver guideline stays unenforced**, now visibly so — the dashboard shows a limit
  the system does not hold, which is more honest than today and still not a control.
- **A second authz copy exists**, free to drift from origination's. Logged as debt; the
  redactor's `redactor-drift` job is the precedent for holding it if it becomes a third copy.
- **`balances` stays a mutable float column.** It is a projection now, but the float and the
  mutability both remain. D2 is narrowed again, not closed.
- **Reads become role-and-owner-scoped, which changes existing behaviour.** Any script or
  demo step calling `/lss/*` with no role, or as a borrower against another loan, stops
  working.
- **A `NULL`-`app_id` loan is staff-only forever**, because no owner is derivable. Correct,
  and invisible until someone asks why a borrower cannot see an old loan.
- **The dashboard is not built by this ADR.** Lending Ops' original ask is still outstanding
  after this work; what changes is that building it no longer ships an unguarded money route.

### Neutral

- One new table and two triggers, matching `decision_events`.
- Four routes gain a header-derived gate; two become internal-only, which ADR 0013 already
  requires for `apply-payment`.
- `approved_by` ships `NULL` on every row until the approval workflow lands, so that build adds
  a write path rather than a migration.
- A manual late-fee reversal is a waiver, not a new route.

---

## Cross-cutting concerns

**Security.** The gate fails closed: an absent or unrecognized role is in neither role set,
and a non-owner is denied as 404 so ids cannot be probed. The trust boundary is unchanged — the
gateway strips inbound `X-User-Id` / `X-User-Role` and re-sets them from the session
(`services/gateway/app/main.py:160-171`), which is what makes the header trustworthy inside servicing;
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

**Operational impact.** No approver roster is needed this cycle, which is the point of the
deferral — the floor keeps working at its measured 45 discretionary moves a week. Lending Ops
sends the ops-manual reason codes; the runbook gains that list, the cutover date, and the
sentence that history begins there. History is queryable from cutover forward and retained for
seven years, which Sam confirms as the control owner.

**Testing impact.** Characterization tests pin today's behaviour before any of this changes
(the companion comprehension PR). New coverage: role and ownership matrix per route, posting
and projection agreeing after each mutation, `before_minor + delta_minor = after_minor` on
every row, a reason required on every manual move, and the append-only triggers refusing
`UPDATE`, `DELETE`, and `TRUNCATE`. Same-person approval is not testable this cycle because
there is no approval path; that test lands with the workflow. The trigger tests need real Postgres,
not SQLite. Expect a dedicated blocking CI job for the authorization matrix, on the model of
`adr-0010-authz-gate` and `kyc-enforcement-gate`.

---

## Implementation plan

Later PRs, in this order. Each is independently mergeable.

1. **Authorization — this cycle, its own PR.** `servicing-service/app/authz.py` plus the
   per-route gates from Decision 1, and the internal-service gate on `late-fee`. No schema.
   Closes D8(b). Lending Ops asked for this one separately and immediately, and confirms the
   endpoints are externally reachable, so it does not wait for anything below it.
2. **Ledger** — migration adding `balance_postings` and its two triggers. **This ADR reserves
   no migration number.** 0013's plan cites 0016–0019 for its own migrations, and `main`
   already holds an unrelated `0016` (`db/migrations/0016_provenance_disclosure_outcome.sql`),
   so any number pinned by either ADR is stale the moment the other lands first. The
   implementing PR must read the current migration head off `db/migrations/` (or `main` at
   merge time, whichever is later) and take the next free number then — not copy a number
   cited by this ADR, 0013, or any other doc. `migration-numbering-gate` enforces the result;
   it does not enforce intent, so this line is the intent. Backfill is not attempted: the
   history does not exist to backfill, and the ledger starts at its first posting with that
   stated in the runbook.
3. **Posting on every mutation** — `balance.py` writes a posting and the projection in one
   transaction. Both `apply_payment` call sites must be covered: the HTTP route
   (`main.py:84`) and the in-process caller (`app/payments.py:79`).
4. **Approval workflow — next cycle, carded.** The request-and-approve flow, its queue, and
   the break-glass path, to the design fixed in Decision 2.
5. **Dashboard** — the original ask, now on top of gated routes and a recorded trail.
6. **Deferred, own ADR:** converting the remaining float money columns (D2), and the
   `audit_logs` append-only fix (D20).

Steps 1–3 are this cycle's control. Step 1 alone removes the Critical finding, so it does not
wait for the ledger. Steps 4 onward, plus borrower notifications, historical reconciliation, and
waiver-limit enforcement, are carded in `docs/cards/week6-servicing.md` at Lending Ops' request
so they are planned rather than dropped.

---

## Rollback strategy

Steps 1 and 3 are code and revert cleanly; behaviour returns to today's, which is why step 1
shipping alone is safe. Step 2 is additive: `balance_postings` can be dropped, or left
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
| A representative moves a balance they should not have, during the cycle before approval exists | Accepted by the client with the volume figures on the record. Mitigated by attribution rather than prevention: actor, before, after, and reason on every posting, and a correcting posting to reverse. The approval workflow is carded, not dropped |
| The deferral becomes permanent — the card is never picked up | It is written down with an estimate, a dependency, an owner, and a start, at the client's own request, and this ADR's Decision 2 states the design so the next cycle starts from a decision rather than a discussion |
| A representative cannot fix a balance quickly and falls back to raw SQL, which no gate sees | Postgres role separation is out of scope here; the mitigation is operational — reconciliation surfaces a direct write as a projection-versus-ledger gap, and it is a reason the ledger ships before the approval flow rather than with it |
| ADR 0013's migrations land in a different order and re-model the payment posting | The seam is stated in Decision 3; whichever ships second adds the missing insert to the existing transaction rather than a second table |
| Free-text reasons carry PII — and free text is the only option until the client's code list arrives | `reason_text` is treated as operator-entered content, not a system field: identifier-free JSONB per ADR 0007 alongside it, and the existing per-service redactor covers the log path. The code list narrows the exposure to the `'other'` case once it lands |
| The `'other'` escape hatch becomes the default and the codes go unused | Report the `'other'` share back to Lending Ops after the first month; a high share means the list is wrong, not that representatives are |

---

## What could supersede this

Converting the remaining float money columns (D2) would make the projection exact and remove
the tolerance from reconciliation. A real identity and enrollment flow would replace the
derived-ownership join with a bound borrower identity, which is the end state ADR 0010 names.
Neither changes the decisions here; both narrow the accepted costs.
