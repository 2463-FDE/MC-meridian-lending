# Servicing money surface — comprehension report (week 6)

**Date:** 2026-08-12 · **Audience:** Lending Ops (Dana Whitfield) and engineering · **Scope:** what
the servicing money endpoints do today, ahead of any dashboard build

Lending Ops asked for a dashboard where representatives adjust balances and waive fees. The
capability already exists as four endpoints, so the dashboard would inherit whatever those
endpoints do. This report is what they do, read out of the code and verified against it. It
changes nothing: the design decisions sit in ADR 0014 (servicing money controls, landing in its own
PR), and no endpoint, schema, or authorization code is touched here.

Every claim below cites `file:line` on this branch. Two tests land with this report:
`services/servicing-service/tests/test_characterization_balance.py` pins the behaviour described
here so a later fix has to change it deliberately, and
`services/servicing-service/tests/test_lost_update.py` demonstrates the concurrency defect and is
expected to fail until that defect is fixed.

---

## Three things stated plainly

Lending Ops asked for these in writing rather than implied.

**1. History starts at cutover and nothing before it can be reconstructed.** There is no record of
who changed a balance, when, by how much, from what, or why. When recording begins, that date is
the beginning of the answerable history; everything before it is not recoverable from what the
system stored. A SOX walkthrough that assumes an adjustment trail exists will not find one.

**2. Today, any signed-in user can waive any amount, on any account.** There is no role check, no
limit, and no second approver anywhere on the servicing money endpoints. That includes borrower
logins, and it includes accounts belonging to other customers.

**3. The ops-manual guideline of $150 per account per month on fee waivers has never been enforced
by the system.** It exists on paper. No code reads it, no column records it, and nothing has ever
refused a waiver for exceeding it. ADR 0014 records and displays it rather than starting to
enforce it; enforcement is a separate carded item.

---

## The money endpoints

`services/servicing-service/app/main.py` and its router, as they stand:

| Route | Line | What it does | Authorization | Record written |
|---|---|---|---|---|
| `POST /payments` | `main.py:59-71` | Inserts a `payments` row (full PAN + CVV), then applies the amount to the balance | authenticated only | `payments` row; no link to the balance change |
| `POST /accounts/{id}/apply-payment` | `main.py:79-85` | Subtracts an amount from `balances.balance` | authenticated only; no internal-service gate | none |
| `POST /accounts/{id}/adjust-balance` | `main.py:101-105` | Sets `balances.balance` to an operator-supplied number | declares `x_user_role` and never reads it | none |
| `POST /accounts/{id}/waive-fee` | `main.py:112-116` | Subtracts an amount from `balances.past_due` | declares `x_user_role` and never reads it | none |
| `POST /accounts/{id}/late-fee` | `main.py:119-121` | Adds a flat $35 to `balances.past_due` | authenticated only | none |
| `GET /accounts/{id}/balance` | `main.py:88-94` | Returns balance and past_due | authenticated only | — |
| `GET /loans/{id}`, `/loans/{id}/payments`, `/loans/{id}/schedule` | `routers/loans.py:61-91` | Loan detail, payment history, amortization | authenticated only | — |

Five of the seven move money or a fee. None of the five writes any record of the movement beyond
overwriting the figure and emitting a log line.

## How a balance changes

`services/servicing-service/app/balance.py:23-32` is the whole mechanism, and the three steps are
labelled in the source:

```python
def apply_payment(loan_id: int, amount: float) -> float:
    current = get_balance(loan_id)             # READ
    new_balance = current - float(amount)      # MODIFY (float, straight off principal)
    db.query(                                  # WRITE (overwrite in place)
        "UPDATE balances SET balance = %s, updated_at = now() WHERE loan_id = %s",
        (new_balance, loan_id),
    )
```

The read and the write are two separate round-trips on a connection that runs
`autocommit = True` (`app/db.py:9-14`). There is no `SELECT ... FOR UPDATE`, no version column, and
no transaction around the pair. `adjust_balance` (`balance.py:35-43`) and `waive_fee`
(`balance.py:46-56`) have the same shape; `delinquency.assess_late_fee`
(`app/delinquency.py:16-25`) has it too, and additionally does not touch `updated_at`, so a late
fee leaves no trace at all — not even a changed timestamp.

`apply_payment` has **two callers**: the HTTP route (`main.py:84`) and `payments.charge` in-process
(`app/payments.py:79`). Any fix has to close both, or the defect stays reachable through the other.

---

## Q1 — Who can move money or waive a fee? Is there a second approver?

**Nobody is gated, and there is no approver.**

The gateway authenticates and forwards an authoritative role. `_proxy_raw` strips any
client-supplied `X-User-Id` / `X-User-Role` and re-sets both from the session
(`services/gateway/app/main.py:160-171`), so a downstream service could trust the header. Servicing
never reads it. The `/lss/*` proxy calls `_require_user` and nothing else
(`gateway/app/main.py:421-425`), as does `/payments` (`:457-464`), and `_require_user`
(`:193-197`) checks only that a session exists.

Inside servicing, `adjust_balance` and `waive_fee` declare `x_user_role: Optional[str] =
Header(None)` (`main.py:103,114`) and never inspect it. The functions they call take no caller
identity at all (`balance.py:35-56`). So all four demo logins — `admin`, `underwriter`, `csr`, and
borrower `maria` — can move money on any `loan_id`. Loan ids are serial integers, so the account
does not have to be known in advance; it can be walked.

**This is reachable from the public internet.** Lending Ops confirms the borrower portal sits
behind the same gateway as the internal application, so a borrower login is an internet-facing
credential. Debt **D8**, Critical, pre-existing since the baseline servicing code.

## Q2 — Does the concurrent payment-plus-waiver example land on the correct final number?

**Not for the reason the brief gives — and the real defect is worse.**

The brief's example pairs a payment with a fee waiver. Those two do not collide:
`apply_payment` writes the `balance` column (`balance.py:27-30`) and `waive_fee` writes `past_due`
(`balance.py:51-53`). Different columns, so concurrent execution of that specific pair produces the
correct figures in both.

The real lost update is **same-column** concurrency — two `apply_payment` calls, or an
`apply_payment` and an `adjust_balance`, on one loan:

| Step | Request A ($100) | Request B ($200) | Stored balance |
|---|---|---|---|
| 1 | reads `500` | | 500 |
| 2 | | reads `500` | 500 |
| 3 | writes `400` | | 400 |
| 4 | | writes `300` | **300** |

The correct result is `200`. The balance lands on `300`, so $100 was captured and never credited.
Whichever request writes last wins, and the earlier delta is lost.

This is not a thought experiment. It was measured on 2026-08-02 against the live stack with
`scripts/repro_double_charge.py`: one $100.00 intent sent eight ways concurrently on loan 4471
produced eight `payments` rows, **$800.00 captured and $600.00 credited** — $200.00 taken and never
applied, with every request returning `200`. It is non-deterministic; a five-way run lost one
application, the eight-way run lost two. Debt **D3**, High, with the fix specified in ADR 0013 and
not yet built.

`test_lost_update.py` demonstrates the same defect deterministically, by forcing both reads to
complete before either write.

## Q3 — Can you reconstruct account 7781's balance history?

**No, and it cannot be backfilled.**

`balances` (`db/init/001_schema.sql:117-122`) is four columns: `loan_id`, `balance`, `past_due`,
`updated_at`. The figures are overwritten in place and `updated_at` is overwritten with them. There
is no actor, no delta, no prior value, and no reason — anywhere.

The other candidates do not help:

- **`audit_logs`** (`001_schema.sql:137-144`) is an ordinary mutable table with a `deleted_at`
  soft-delete column, so its rows can be updated and removed. No servicing code writes to it
  (`grep -rn "audit_logs" services/servicing-service/` returns nothing). It would not be
  trustworthy as an audit trail even if it were populated. Debt **D20**.
- **`payments`** (`001_schema.sql:125-134`) records card charges only. It has no idempotency key
  and no link to the balance delta it caused, and the manual paths — `adjust-balance`, `waive-fee`,
  `late-fee` — and the split-flow `apply-payment` write no row here at all.
- **Log lines.** `balance.py` emits `log.info("adjusted balance loan_id=%s %s -> %s", ...)`, which
  does contain the before and after figures. It is not queryable, not retained as a record, carries
  no actor, and is not an audit artifact.

So the answerable depth today is: the current balance, the current past_due, and a last-modified
timestamp. Nothing else, and nothing historical.

**The sentence for the record, in the client's words:** history starts at cutover and nothing
before it can be reconstructed.

The one table in this schema that is genuinely append-only is `decision_events`
(`001_schema.sql:148-179`) — serial primary key, JSONB payload, a `BEFORE UPDATE OR DELETE` trigger
and a `BEFORE TRUNCATE` trigger that both raise. It is the pattern a balance ledger should copy,
and ADR 0014 does.

## Q4 — Which actions are money-affecting, and which need an approver and a ledger row?

| Action | Effect | Needs a ledger row | Needs an approver | Role expectation |
|---|---|---|---|---|
| `POST /payments` | inserts a payment, reduces `balance` | yes | no — the borrower chose the amount | internal / borrower capability |
| `apply-payment` | reduces `balance` | yes | no — amount comes off a captured payment | internal service only |
| `adjust-balance` | sets `balance` to any figure | yes | **yes** — an operator chooses the number | CSR or admin |
| `waive-fee` | reduces `past_due` | yes | **yes** — an operator chooses the amount | CSR or admin |
| `late-fee` | adds $35 to `past_due` | yes | no — rule-driven | internal service only |
| reads | none | n/a | n/a | staff, or the owning borrower |

The principle: **every mutation writes a ledger row; an approver binds only the moves where a human
chooses the amount.** Automated movements are recorded but not approved, because there is no
operator decision for a second person to check.

One case the classification has to absorb: representatives do sometimes reverse a late fee by hand.
That is a discretionary move, so it goes through the waiver path and gets that path's record and
reason. There is no separate manual late-fee flow.

*(ADR 0014 decides that the approver arrives next cycle and the record arrives now. This report
classifies the actions; it does not decide the sequencing.)*

---

## Debt ties

| Debt | Entry | Bearing on this surface |
|---|---|---|
| **D8** | Servicing enforces no authorization (IDOR + no maker-checker) | Q1. Critical, pre-existing, internet-facing per the client's reachability answer |
| **D3** | Unlocked read-modify-write on `balances` | Q2. High, measured, fix specified in ADR 0013 and not built |
| **D2** | Float arithmetic for money | Every figure on this surface is `DOUBLE PRECISION`; `balances` is a single mutable column with no ledger |
| **D20** | `audit_logs` is mutable and seeded with a plaintext PAN | Q3. Why the existing "audit" table cannot answer the history question |

One bookkeeping note for anyone reading the source: the D-numbers in servicing's comments have
drifted from `docs/debt-log.md`. `main.py:68` cites "debt D2" for the missing idempotency key, which
the log defines as D19; `main.py:83` cites D14 for the absent payment waterfall, which the log
defines as encoded-PII log bypass. The log's own bookkeeping note records this drift. Trust the log,
not the comments.

## Out of scope for this report

No dashboard. No endpoint, authorization, or schema change. No ledger DDL or migration. No fix to
`balance.py`. Those follow from ADR 0014 and its implementation plan; the only code landing with
this report is the two test files, which assert current behaviour and change none of it.
