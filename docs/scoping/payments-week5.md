# Week 5 — Problem Scoping: "Add a payment form"

**Client:** Dana, VP of Lending Ops, Meridian Lending Co.
**Date:** 2026-08-02
**Branch:** `feature/payments-week5` (cut from `main` @ `170ed29`)
**Deliverable:** spec package. Implementation is scoped, not built.
**Companion docs:** `docs/specs/payments-week5.md` (acceptance criteria, design, test vectors),
ADR 0013 (idempotency + tokenization decision, supersedes ADR 0003).

---

## 1. The ask, as received

> Customers want to pay online — card and ACH. Let's just add a payment form so they can
> self-serve. We've had a few "I was charged twice" complaints, but I think people are just
> confused. I attached the prototype handler the last vendor started. Keep it simple.

Two claims are embedded in that ask, and both are wrong:

1. **"People are just confused."** They are not. The duplicate charges are two successful
   database inserts from one customer intent. The evidence is in the code, not in the
   support queue.
2. **"Just add a payment form."** The form is the smallest part. A self-serve form changes
   *who* initiates a money movement — from a Meridian employee to a member of the public —
   and the layer that money movement lands on has no authorization at all.

This document reframes the ask against the code as it stands on `main`.

---

## 2. Ground truth: what survived contact with the code

Every claim in the handover was checked against `main` (the client's actual production
state), not against a feature branch.

| Handed-over claim | Verdict | Evidence on `main` |
|---|---|---|
| One retried POST produced two `payments` rows | **TRUE** | `services/payment-service/app/payments.py:74` — `# No idempotency check. No unique charge reference. Every POST inserts a row.` |
| Request carries no idempotency key / request id | **TRUE** | `services/payment-service/app/schemas.py` `PaymentIn` has no key field; `routers/payments.py:27` confirms none is accepted |
| A retry double-charges across two services | **TRUE, and understated** | see §3.2 |
| `payments.py` stores the full PAN | **TRUE** | `payments.py:76-78` — `INSERT INTO payments (loan_id, pan, cvv, ...)`, annotated `# full PAN + CVV persisted` |
| DDL has no tokenization, no unique on the charge reference | **TRUE** | `db/init/001_schema.sql:113-122` — `pan TEXT`, `cvv TEXT`, `-- no idempotency_key, no unique(charge_ref)` |
| `payments.py` logs the CVV at INFO | **FALSE — already fixed** | see §2.1 |

### 2.1 One handed-over fact is stale

The CVV is no longer logged. `charge()` builds its log line through
`_redacted_charge_req` (`services/payment-service/app/payments.py:43-72`), which masks the
PAN by digit count, replaces the CVV with `••••`, masks the SSN to last-4, and deliberately
omits the client-controlled `name` field. Masking happens at the *value* level before
interpolation, with the log formatter retained as a backstop. This landed in the Week 1 PII
work and is on `main` as of PR #9.

What misled the client is the **module docstring**, which still reads *"Logs the full charge
request (PAN, CVV, SSN) at INFO"* (`payments.py:1-9`). The same stale claim appears in
`services/payment-service/app/main.py:1-7`. The team read the docstring, not the function.

This matters beyond bookkeeping: it is the difference between a *logging* remediation
(done) and a *storage* remediation (not done, and the higher-severity one). Correcting the
docstrings is a line item in the spec.

---

## 3. The reframe

"Add a payment form" is, accurately stated, five problems. Two of them are the deliverable.
One is a precondition. Two are named and bounded so they do not silently expand the week.

```
   Dana's ask:            "just add a payment form"
                                   |
   What it actually is:   +-- 1. Idempotency + concurrency  (IN SCOPE — the double charge)
                          +-- 2. PCI scope                  (IN SCOPE — SAD storage is prohibited)
                          +-- 3. Authorization              (PRECONDITION — self-serve has no authz)
                          +-- 4. No ledger                  (NAMED, BOUNDED — no reversal path)
                          +-- 5. Card and ACH are two rails (NAMED, PARTLY IN — state model only)
```

### 3.1 Problem 1 — Idempotency and concurrency

The retry path is exactly as described: a slow `POST /payments` (2.4s), a client retry
410ms later, two inserts. Neither the API nor the database can tell the two apart, because
nothing in the request identifies the customer's *intent* as opposed to the *attempt*.

#### Reproduced, not inferred

`scripts/repro_double_charge.py` drives the real `POST /payments` path through the gateway
against the running stack and counts rows and balance movement. Run against loan 4471 for a
single $100.00 payment intent, on code byte-identical to `main`:

```
A. RETRY (2 sequential POSTs, one intent)
   payments rows made : 2      DEFECT
   captured           : 200.00   expected 100.00
   credited to loan   : 200.00

B. CONCURRENT (8 simultaneous POSTs, one intent)
   payments rows made : 8      DEFECT
   captured           : 800.00   expected 100.00
   credited to loan   : 600.00   DEFECT (lost update)
   response codes     : [200 x 8]   — every attempt reported success
```

The tickets are not confusion. **One intent, eight charges, all reported successful.**

The concurrent case also exposed a second, independent defect that no amount of idempotency
work would fix — see (d).

Four findings make this larger than "add a key":

**(a) There are two live charge handlers, not one.** The ADR 0004 decomposition copied the
payment handler into `payment-service` and **left the original in place**:

- `services/payment-service/app/payments.py:76` — `INSERT INTO payments (...)`
- `services/servicing-service/app/payments.py:75` — `INSERT INTO payments (...)`

Both are routed and reachable (`payment-service/app/routers/payments.py:19`,
`servicing-service/app/main.py:60`). Both carry the identical
`# No idempotency check` comment. An application-level dedupe added to one service leaves
the other writing unguarded rows into the same table. **This is the argument for the
uniqueness constraint living in the database rather than in application code**: the
constraint binds every writer, including the one you forgot about and including the direct
SQL a support engineer runs at 2am.

**(b) The cross-service split has a silent failure state.** `payment-service` captures the
charge, then calls servicing over HTTP to apply it
(`payment-service/app/payments.py:83`, `:92-110`). The `except Exception` at `:104` logs the
failure and returns anyway — the function still reports `"status": "captured"` (`:87`). So
a servicing timeout produces: card charged, `payments` row written, balance possibly
unmoved, and a success response to the customer. There is no reconciliation job that closes
this. The double-charge is the *visible* half of the split; charged-but-unapplied is the
invisible half, and no support ticket will ever describe it correctly.

**(c) The cheap half is already half-built.** `payment-service` passes `payment_id` to
servicing's apply endpoint, and `apply_payment` accepts it
(`servicing-service/app/main.py:80-85`) — then ignores it. `payment_id` is unique per row,
so servicing-side dedupe is a unique index plus a replay branch. The expensive half is the
customer-facing edge, which carries no key at all.

**(d) Concurrency also loses balance updates, and idempotency does not fix that.** This one
came out of the reproduction rather than the code read, and it is the most costly finding in
this document.

At 8 simultaneous requests the run captured $800.00 and credited only $600.00 — **$200.00
taken and never applied to the loan**. The cause is separate from idempotency:
`balance.apply_payment` performs an unlocked read-modify-write, annotated as such in the
code (`servicing-service/app/main.py:80-85`, *"still does the unlocked read-modify-write"*).
Concurrent applies all read the same opening balance and overwrite one another, so the last
writer wins and the rest of the money vanishes. The loss is non-deterministic — a 5-way run
lost one application, an 8-way run lost two.

The two defects compound, and both compound *against the customer*:

| | Effect |
|---|---|
| Duplicate capture (no idempotency key) | the customer is charged N times for one payment |
| Lost update (unlocked read-modify-write) | fewer than N of those charges reach the loan |
| Together | the customer pays more and owes more than they should, simultaneously |

And with no ledger (§3.4) there is nothing to reconcile it against.
`servicing-service/app/reconciliation.py:14` sums the `payments` table, so the discrepancy
is arithmetically visible — $800 of payments against $600 of balance movement — but nothing
acts on it and nothing can attribute it to a specific customer after the fact.

**A unique index on the idempotency key does not close this.** It collapses N duplicate
*intents* into one, which removes the trigger, but two genuinely distinct payments applied
concurrently still race. The fix is that the balance mutation must be a single atomic
statement (`UPDATE ... SET balance = balance - :amount`) inside the same transaction as the
application record, never a read followed by a write. That requirement is now explicit in
the spec (D3d).

An idempotency design must therefore answer, at minimum:

- Who mints the key — client or server — and what it is scoped to (customer intent, not
  HTTP attempt).
- What a replay returns: the **original** response body, not a `409`. A retry that gets an
  error looks like a failure and provokes a third attempt.
- What same-key-different-payload returns (a conflict — it is a client bug, not a replay).
- What two *concurrent* same-key requests do. This is not the retry case: both pass any
  read-then-check in the application, so only the database constraint arbitrates.
- How the key propagates across the payment-service → servicing hop so one intent produces
  one balance movement.
- Key retention: how long a key is honoured, and what happens after it expires.
- Separately from all of the above: how the balance mutation itself is made atomic, since
  (d) shows the key alone leaves money unaccounted for.

### 3.2 Problem 2 — PCI scope

Two distinct issues sit under "stores the card":

**Storing the CVV is a flat prohibition.** Sensitive authentication data may not be
retained after authorization under any circumstances — there is no compensating control
that permits it, unlike PAN storage which is permitted-but-expensive. The `cvv TEXT` column
(`db/init/001_schema.sql:117`) and both INSERT statements put Meridian in direct violation
today, for every payment since go-live. **The remediation is a deletion, not a build**, and
it is the highest-severity item in the week.

**Storing the PAN in plaintext is scope-growing.** `pan TEXT`, no tokenization, no
encryption, no key management. Every backup, every replica, every `pg_dump` on a laptop is
in scope for assessment.

It compounds outward: the seed data writes plaintext PANs *and CVVs* into `payments`
(`db/init/002_seed.sql:68-74`, `003_seed_bulk.sql:74`) and a plaintext PAN into
`audit_logs.detail` (`002_seed.sql:79`, debt D20), so even a demo database is carrying
cardholder data.

**The scope-shrinking opportunity.** This is the part of the ask worth arguing back to
Dana. A self-serve form is the *first* time Meridian has a browser in the payment path, and
therefore the first opportunity for the PAN never to reach a Meridian server at all —
collected in a processor-hosted field, exchanged for a token client-side, with only the
token, brand, and last-4 crossing into the backend. Built the naive way, the form
permanently *grows* the assessment footprint by adding a public entry point to a
PAN-storing system. Built this way it shrinks it, and the shrink is only available because
the form is being built now. That is the strongest version of "keep it simple" — simple to
*assess*, not simple to type.

**This supersedes ADR 0003** (`adr/0003-store-card-data-for-convenience.md`), which decided
to keep the card on file so that support can "see the card on file" when a borrower calls
and finance can re-run a charge without re-asking. Those are real requirements and the
replacement design has to answer them, not delete them:

| ADR 0003 requirement | Answered by |
|---|---|
| CSR "sees the card on file" | brand + last-4 + expiry, stored locally; sufficient for caller verification |
| Finance re-runs a charge | processor token — a card-on-file charge against the stored token |
| *(implicit)* the CVV is needed to re-charge | false; CVV is not used in card-on-file transactions, which is why retaining it buys nothing |

### 3.3 Problem 3 — Access to the money-movement layer

Absent from the handover entirely. Three separable things get called "auth" here, and they
have very different costs. Keeping them separate is most of the work of scoping this week.

#### (a) A missing trust-boundary check — in scope, and not an authorization model

`POST /lss/accounts/{id}/apply-payment` (`servicing-service/app/main.py:80`) reduces a loan
balance. It is reachable through the gateway by any authenticated session
(`services/gateway/app/main.py:421-425`), and it has **no internal-service gate**. It never
verifies that a payment was captured. A caller can credit a balance with no card, no
`payments` row, and no processor contacted. That is not information disclosure — it is
money creation.

This needs no identity model, no roles, and nothing from ADR 0010. It is the same header
check three sibling services already ship: `kyc-service/app/routers/kyc.py`,
`decision-service/app/routers/decisions.py`, and `disclosure-service/app/routers/offers.py`
each require `X-Internal-Service`. Servicing and payment-service check nothing (verified
across both trees). An internal-only endpoint was left publicly routed when ADR 0004 split
the services. Hours of work, on the exact endpoint this feature calls — so it is a
deliverable, not a precondition.

#### (b) RBAC — deferred, and correctly so

- **Borrower-owns-the-loan on `POST /payments`.** The harm is thinner than it first looks:
  paying a *stranger's* loan with your own card gives money away, it does not take any. The
  real leak is balance disclosure in the response — and `GET /lss/loans` already hands the
  whole portfolio to every caller regardless. `frontend/app/my-loan/page.tsx:50-53` filters
  to the logged-in borrower **client-side only**, and says so in a comment. Shipping the
  payment form neither creates nor widens that.
- **`adjust-balance` and `waive-fee` officer-only** (`servicing-service/app/main.py:102`,
  `:113`). Real, pre-existing, unrelated to taking a payment.

Both are **D8**, both are the intended scope of **ADR 0010**, which was implemented for
origination and deliberately deferred for servicing. They stay deferred. Neither blocks
this feature. Note also that §3.3(c) makes the first one largely moot.

#### (c) How self-serve access actually works — capability token, not signup

The instinct is that self-serve needs borrower accounts. It does not, and this repo has
already proven the alternative on a different noun.

**ADR 0010 Phase B — the continuation token** (`db/migrations/0008`, hardened by `0009`
after PR #7 review) lets an anonymous applicant complete their own decision/offer/accept
with no login:

| Property | As shipped for applications |
|---|---|
| Scope | one application id, one flow — *"a capability scoped to one application, not a session"* (`0008` header) |
| At rest | keyed hash only (`origination-service/app/authz.py:62` `hash_token`); a DB dump or logged row yields a non-replayable digest |
| Lifetime | `continuation_token_expires_at`; rejected if NULL or past. `CONTINUATION_TOKEN_TTL_DAYS`, default 7 (`origination-service/app/config.py:292`) |
| Single-use | cleared to NULL at the money action (`accept_offer`) |
| Browser exposure | none. Gateway stores the raw token in Redis under an opaque session id; the browser gets only that id in an HttpOnly, SameSite=Strict, path-scoped cookie (`gateway/app/main.py:48-50`, `auth.py:create_resume_session`). localStorage was explicitly rejected as XSS-reachable. |
| Transport inward | gateway re-attaches it as `X-Application-Token` when proxying |

Swap the noun and this is a payment capability: scope `pay:loan:{id}` instead of
`application:{id}`. Everything else — hashing, expiry, cookie indirection, the review scars
from PR #7 — is inherited rather than rebuilt. It is the second use of an established
pattern in this codebase, not a new abstraction.

**A capability is strictly narrower than a session.** A session grants ambient authority
over everything the role can do; this grants one loan and one verb, and expires. Ownership
stops being a runtime lookup and becomes an invariant of the token, minted server-side by
the system that already knows who owns the loan. There is no serial id to walk, so §3.3(b)'s
first item largely dissolves.

**The real dependency is a delivery channel, not an identity model.** Both migrations say
so in their own words — *"there is no verified email/SMS channel in this platform"* — which
is why `0008` refuses to backfill tokens and `0009` refuses to rehash in place: an
undelivered token is false safety. For origination this never bit, because the applicant
had *just submitted the form* and the browser was right there to receive the token in
session. **Payments have no in-session moment.** The borrower returns weeks later with no
browser state, so the token must arrive out-of-band.

**Cold enrollment is the wrong shape; enrollment at sanction is not.** The obvious
alternative — "let the borrower register with their loan number, last-4, and date of
birth" — has to re-prove identity from knowledge factors that a data broker also has. But
**enrollment offered at loan sanction** is a different proposition entirely, and it is the
one worth building toward:

- The borrower is **in session**. They have just accepted the offer; the browser is right
  there. This is the same property that makes origination's continuation token work and
  that payments otherwise lack.
- Identity is **already proven**. KYC/CIP ran at intake (`kyc-service`) and the borrower
  has just accepted a TILA disclosure. That is the strongest identity assertion this
  platform will ever hold about that person; enrollment piggybacks on it instead of
  reconstructing it.
- It is the right moment to **capture and verify an email**, sent and confirmed while the
  borrower is still in session — which supplies, going forward, the channel Q7 is missing.

What it does not do is serve anyone who already has a loan. Existing borrowers never pass
through sanction again, so on launch day enrollment covers roughly none of the payment
volume, growing only as new loans board — while the double-charge is affecting borrowers
paying today.

It also carries two prerequisites that the capability token does not:

- `users.password_hash` is **unsalted sha256** (`gateway/app/auth.py:31`, a brownfield
  caveat kept on purpose). Enrolling members of the public against that means one database
  leak exposes every borrower's password, and borrowers reuse passwords. Argon2 (or bcrypt)
  plus rehash-on-login has to land first. Bounded work, and worth doing regardless.
- **A session is ambient authority; a capability is not.** Issuing borrower sessions makes
  the §3.3b RBAC work a hard prerequisite — `GET /lss/loans` currently returns the whole
  portfolio to any caller and `adjust-balance` accepts anyone. The capability token avoids
  that entirely by never granting anything beyond one loan and one verb.

**Position — a three-phase access model, not a single choice:**

| Phase | Access model | Serves | Prerequisites |
|---|---|---|---|
| **1** | Staff-assisted: a CSR takes the card while the borrower is on the phone, on existing logins. | 100% of the book, immediately | None. Ships with the idempotency + PCI work. |
| **2** | **Pay-by-link**: statement or due-date notice carries a `pay:loan:{id}` capability token. Delivery channel is the identity proof — the same bar as mailing a paper statement, and the industry-standard bar for inbound payment. | Back book, self-serve | A verified channel (Q7). Reuses `0008`/`0009` wholesale. |
| **3** | **Enrollment at sanction**: borrower opts into an account at offer acceptance, on the back of the CIP check just completed. | New loans, full account | Argon2 password hashing; **ADR 0010** RBAC; email verification. |

Phase 2 is not throwaway work once phase 3 exists. Pay-by-link permanently serves the back
book, and "pay without logging in" is a path lenders keep indefinitely.

Phase 3 is the target end state rather than a rejected option, because **accounts are what
autopay needs**: recurring payment, a saved card-on-file token, and payment history are
worth considerably more to Dana than one-off payments, and they are where the stored
processor token from §3.2 finally earns its keep.

This week specifies phases 1 and 2, and the *seam* for phase 3 — the stored token and the
event-shaped payment record are exactly what an autopay schedule will later read. It
deliberately keeps the parts that are broken *today*, for payments Meridian is already
taking, off the critical path of a channel procurement decision or an identity programme.
**Assumption recorded:** a verified email channel exists or can be procured; see Q7 in §6.

#### (d) What pay-by-link costs, and what closes it

A mailed capability link is not free. The risks below are what the spec's design rules in
D6 exist to answer; three of them change the design rather than merely being accepted.

| # | Drawback | Why it bites | Mitigation |
|---|---|---|---|
| 1 | **Link scanners consume the token** | Corporate mail filters (Safe Links, Proofpoint and similar) fetch every URL in an email before the recipient sees it. A token that is spent on `GET` is dead by the time the borrower clicks, and the failure looks like an outage. | **Hard rule: `GET` is non-consuming and side-effect-free.** Only `POST /payments` spends anything. Stated as a design rule so it survives later edits. |
| 2 | **Phishing symmetry** | The model trains borrowers that a mailed link opening a payment page with no login is normal Meridian behaviour — which is exactly the pattern a phishing mail imitates. | **No clean technical fix.** Consistent sending domain, no link shorteners, and customer messaging reduce it. This is the honest reason phase 3 is the target end state rather than a nice-to-have. |
| 3 | **The link is a pure bearer credential** | Forwarded mail (spouse, accountant), a shared device, browser history, or a compromised mailbox all yield a working capability. Paying a stranger's loan is not theft, but the landing page displays balance and payoff amount — a financial-data disclosure. | **Require one knowledge factor at the landing page** (date of birth, ZIP, or last-4). Converts bearer-only into link + something-you-know, and matches what borrowers already expect from a lender portal. |
| 4 | **Statement generation becomes credential issuance** | The statement batch stops being a reporting job and starts minting and mailing capabilities. A batch defect that mails borrower A's link to borrower B hands out a live credential. | Treat the batch at the same bar as password-reset mail: review, logging, and an explicit blast-radius assessment. Budget it as security-relevant code, not reporting code. |
| 5 | **Bulk revocation** | If a mail batch goes wrong, thousands of tokens must die at once. Hashing at rest protects confidentiality; it is not a kill switch. | A per-loan `payment_token_version`, bumped to invalidate every outstanding link for that loan; bulk-bumpable for a whole batch. Cheap designed in, awkward retrofitted. |
| 6 | **Expiry tension** | A short lifetime produces dead links and support calls; a long one widens the exposure window. Late payers hit the dead link exactly when they most need to pay. | One statement cycle, configurable (Q8). A dead link lands on a page that offers the phase-1 phone path rather than an error. |
| 7 | **Deliverability enters the collections path** | A link in a spam folder means a missed payment, a late fee, and a dispute Meridian will lose. | Out of scope to solve; named so that whoever owns dunning knows email delivery is now on the critical path. |
| 8 | **No consolidated view** | Two loans means two links, no payment history, no autopay. | Accepted. It is a phase-3 capability and part of the argument for it. |
| 9 | **Borrowers without usable email** | A non-trivial slice of consumer installment borrowers. | Accepted — phase 1 (staff-assisted) is permanent, not transitional. |

Already handled by the inherited pattern: the raw token leaves the URL on first contact —
the gateway exchanges it for an opaque Redis session id delivered in an HttpOnly,
SameSite=Strict, path-scoped cookie (`gateway/app/main.py:48-50`) — and it is stored only
as a keyed hash (`authz.hash_token`). Adding `Referrer-Policy: no-referrer` on the landing
page closes the remaining URL-leak surface.

**Net:** three additions to the inherited design — non-consuming `GET`, a knowledge factor
at the landing page, and a bulk-revocation version column. Item 2 has no fix, and is
recorded as an accepted risk with phase 3 as its answer.

**One separation to state explicitly, because conflating it is the tempting shortcut:**

- The **capability token** answers *may this caller pay this loan*. It lives for the
  statement window and survives multiple attempts, so a declined card does not kill the
  link.
- The **idempotency key** answers *is this the same payment intent*. It is minted per form
  submission and expires in hours.

Using a single-use token as the dedupe mechanism collapses the two and breaks on the first
declined card.

### 3.4 Problem 4 — No ledger (named, bounded)

`balances` is a single mutable `DOUBLE PRECISION` column with no ledger behind it (debt
**D2**), mutated by an unlocked read-modify-write (`servicing-service/app/main.py:80-85`
calls `balance.apply_payment`, annotated *"still does the unlocked read-modify-write"*).

That read-modify-write is no longer a theoretical concern: §3.1(d) measured it losing
$200.00 of $800.00 captured in a single 8-way concurrent run. The atomicity fix is now in
scope (spec D3d) even though the ledger is not.

Four consequences for this feature:

- **A double charge cannot be reversed as a record.** There is no entry to contra; you can
  only overwrite the balance to a number someone believes is right. Reconciliation sums the
  `payments` table (`servicing-service/app/reconciliation.py:14`) and cannot distinguish a
  duplicate from a genuine second payment.
- **Captured-but-not-credited money cannot be attributed.** The §3.1(d) run leaves the
  `payments` total and the balance movement disagreeing by $200. Reconciliation can see the
  aggregate gap; nothing can say which customer it belongs to, because the fact that an
  application happened is not recorded anywhere.
- **"Was I charged twice?" is not answerable from data.** This is a real reason the three
  tickets were dismissible as confusion — nobody could cheaply prove otherwise.
- **Refund, void, and ACH return have nowhere to live.** They are not balance edits; they
  are events.

The ledger is not solved this week. The spec **does** require that the payment state model
be event-shaped so a ledger can be introduced later without re-modelling, that it add no new
mutable money columns, and that the balance mutation be atomic — the last of which is the
minimum needed to stop money disappearing while the ledger waits.

### 3.5 Problem 5 — "Card and ACH" is two rails (named, partly in scope)

The ask treats these as one form with a dropdown. They are different systems with different
rules and, critically, different *success* semantics:

| | Card | ACH |
|---|---|---|
| Regime | PCI DSS | NACHA |
| Sensitive data | PAN, CVV (SAD) | account + routing number — sensitive, but **not** cardholder data; different storage rules |
| Lifecycle | authorize → capture | submit → settle |
| "Success" means | funds are held/captured | the file was accepted, **not** that money moved |
| Reversal window | chargeback | returns (R01 insufficient funds, R10 unauthorized) arriving up to 60 days later |

The current code has one verb — `charge()` — returning `"status": "captured"` for both,
selected by a `method` string that defaults to `"card"`
(`payment-service/app/schemas.py` `PaymentIn.method`). Reporting an ACH submission as
`captured` and immediately reducing the balance means the balance is wrong for every ACH
payment that later returns, and Meridian finds out weeks afterwards.

**In scope:** the payment state model must distinguish submitted / settled / returned and
must not treat ACH submission as funds received. Getting these states wrong is what
generates the *next* round of "your system charged me wrong" tickets, and the state names
are nearly free to get right at spec time and expensive to change later.

**Out of scope:** the return-handling implementation, the settlement reconciliation job, and
the NACHA file format work.

---

## 4. Scope statement

**In scope (this week, spec only):**

1. Idempotency-key design for `POST /payments` — key semantics, replay behaviour, conflict
   behaviour, cross-service propagation, retention.
2. Database-level uniqueness and transaction boundary that makes a duplicate charge
   impossible regardless of which handler or client is calling.
3. PCI design: eliminate CVV storage; tokenize the PAN; define what may be retained
   (token, brand, last-4, expiry) and the browser-side collection model that keeps the PAN
   off Meridian servers.
4. Payment state model covering both rails, event-shaped, with ACH submission distinct from
   funds received.
5. **Atomic balance mutation** (§3.1d, §3.4) — a single `UPDATE ... SET balance = balance -
   :amount` inside the same transaction as the application record, replacing the unlocked
   read-modify-write that was measured losing money under concurrency. Not the ledger; the
   minimum that stops captured money going unapplied.
6. `X-Internal-Service` gate on `apply-payment` (§3.3a) — closes the money-creation path.
   Not RBAC; the same check three sibling services already ship.
7. Self-serve access model: a `pay:loan:{id}` capability token extending the ADR 0010
   Phase B continuation-token pattern (§3.3c) — scope, hashing, expiry, cookie indirection,
   revocation, and its separation from the idempotency key.
8. Acceptance criteria.
9. Test vectors: retry, concurrent duplicate, key-reuse-with-different-payload,
   cross-service partial failure, lost-update, tokenization, capability-token
   scope/expiry, and no-SAD-anywhere assertions.
10. **A live reproduction** (`scripts/repro_double_charge.py`) demonstrating both defects
    against the running stack, so the reframe is evidence rather than assertion.
11. ADR 0013 recording the decision and superseding ADR 0003.

**Named and bounded (not solved this week):**

12. RBAC on the servicing layer (§3.3b) — borrower-owns-the-loan on reads, officer-only on
    `adjust-balance` / `waive-fee`. Stays with **D8** / **ADR 0010**. Does not block this
    feature; §3.3c's capability token makes the payment-path half of it largely moot.
13. Ledger / reversal accounting (D2). Constraints imposed: the state model must be
    event-shaped, must add no new mutable money columns, and the balance mutation must be
    atomic (item 5).
14. ACH return handling, settlement reconciliation, NACHA file format.
15. Consolidating the two duplicate charge handlers into one service. The DB constraint
    covers the correctness risk in the meantime; the duplication is a maintenance cost.
16. Procuring or verifying the email/SMS delivery channel the pay-by-link model depends on
    (§3.3c, Q7). Staff-assisted payment is the phase that ships without it.

**Explicitly not in scope:**

17. Borrower enrollment / password credentials. Rejected in §3.3c: more work, same channel
    dependency, plus a credential store on top of unsalted sha256 hashes.
18. Converting existing float money columns to Decimal outside anything this design adds.
19. Backfilling or re-tokenizing historical `payments` rows — that is a data migration with
    its own customer-communication and assessment implications. The spec names the trigger
    and the shape; it does not schedule it.

---

## 5. What "keep it simple" should mean here

Dana's constraint is legitimate and worth honouring literally rather than dismissing. The
simplest *system* is not the smallest diff:

- Not storing the PAN is simpler than storing it — no key management, no encryption at
  rest, no retention policy, a smaller assessment.
- Not storing the CVV is simpler than storing it — one fewer column, and it was never used.
- One unique constraint in the database is simpler than dedupe logic in two services that
  must stay in step.
- An internal-only `apply-payment` is simpler than reasoning about who is allowed to call a
  public one.
- A capability token scoped to one loan is simpler than borrower accounts — no password
  store, no reset flow, no account recovery, no session to steal, and the authorization
  question is answered at mint time instead of on every request.

Each of these is a *smaller* system than what exists today. The complexity Dana is right to
resist is elsewhere: a full ledger, a full identity model, ACH return automation. Those are
deferred above, by name.

---

## 6. Questions for Dana

Answers change the design; assumptions are recorded so a different answer costs a
configuration change rather than a rebuild.

1. **Processor.** Which card processor, and does it offer browser-side tokenization
   (hosted fields / client-side token exchange)? This decides whether PCI scope can shrink
   or merely be contained. *Assumption: a processor with hosted fields is available.*
2. **Historical card data.** There are stored PANs and CVVs for every payment since
   go-live. Purging the CVV is not optional and should not wait for the rest of this work.
   Who owns that decision and when does it happen? *Assumption: CVV purge is scheduled
   independently and ahead of the form.*
3. **Card on file.** Does the CSR "see the card on file" requirement from ADR 0003 survive?
   Brand + last-4 covers caller verification; if someone needs more, say now.
   *Assumption: brand + last-4 + expiry is sufficient.*
4. **ACH timing.** When a borrower submits an ACH payment, what should they see — "payment
   submitted" or "payment received"? The honest answer is submitted, and it changes the
   product copy. *Assumption: submitted, with settlement confirmed separately.*
5. **Duplicate window.** How long should a retry be recognised as the same payment — minutes
   (a network retry) or days (a customer clicking again tomorrow)? These are different
   products. *Assumption: 24 hours, configurable.*
6. **The three existing tickets.** Were those customers refunded? If not, remediation is a
   business action, not a code change, and it is outstanding now.
7. **Delivery channel — the one that decides the launch shape.** Pay-by-link needs a
   verified way to reach the borrower's contact on file. Migrations `0008` and `0009` both
   record that this platform has none. Does Meridian send statements or due-date notices
   today, and by what channel? *Assumption: a verified email channel exists or can be
   procured; until it is confirmed, staff-assisted payment is the phase-one launch and the
   PCI + idempotency work ships regardless (§3.3c).*
8. **Link lifetime.** How long should a pay-by-link stay live — one statement cycle, 30
   days, until the next statement supersedes it? *Assumption: one statement cycle,
   configurable, following the `CONTINUATION_TOKEN_TTL_DAYS` precedent.*

None of these block writing the spec. Q7 changes the launch sequence, not the design.

---

## 7. Debt log impact

| Debt | Effect of this week |
|---|---|
| **D19** — payment double-charge, no idempotency key | Directly addressed by the design. |
| **D13** — PAN and CVV stored in database | Directly addressed. CVV storage removed; PAN tokenized. Historical rows are a separate migration (Q2 above). |
| **D5** — plaintext PII in logs | Already remediated for the charge path (§2.1). Stale docstrings corrected. |
| **D8** — servicing enforces no authorization | **Partly addressed.** The `apply-payment` money-creation path is closed by the internal-service gate (§3.3a, in scope). The RBAC half — ownership on reads, officer-only on `adjust-balance`/`waive-fee` — stays open under ADR 0010 (§3.3b). |
| **D2** — float money, no ledger | **Partly addressed, and upgraded from theoretical to measured.** The unlocked read-modify-write in `balance.apply_payment` lost $200 of $800 captured in one 8-way concurrent run (§3.1d) — money taken and never credited. The atomicity fix is in scope; the ledger and the float columns are not. The state model is constrained to be event-shaped and to add no mutable money column. |
| **D20** — `audit_logs` mutable, seeded plaintext PAN | Seed PAN scrub pulled into scope alongside the `payments` seed rows (same change, same files). Append-only trigger remains out. |
| **ADR 0010** | Extended in pattern, not in scope: §3.3c reuses the Phase B continuation-token design for a `pay:loan:{id}` capability. The ADR's own deferred item — a verified delivery channel — becomes this feature's launch-sequencing dependency (Q7). |
| **New** — two live charge handlers writing the same table | To be logged as a new debt entry (maintenance risk; correctness risk covered by the DB constraint). |

---

## 8. Status

Sections 1–7 complete. Next: `docs/specs/payments-week5.md` (acceptance criteria, design,
test vectors) and ADR 0013.
