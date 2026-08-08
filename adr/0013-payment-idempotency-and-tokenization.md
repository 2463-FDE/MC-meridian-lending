# ADR 0013: Payment Idempotency, Cardholder-Data Tokenization, and Self-Serve Access

- **Status:** **Proposed** — spec week. Implementation is scoped, not built.
- **Date:** 2026-08-02
- **Author:** Claude Code
- **Supersedes:** **ADR 0003** (store full card data on the payment record). Its stated
  business needs are carried forward and answered differently; see *Decision 2*.
- **Related:** ADR 0010 (application ownership + continuation token — the capability pattern
  reused in Decision 3), ADR 0004 (the decomposition that duplicated the charge handler),
  ADR 0012 (Decimal / minor units precedent), ADR 0002 (single shared database).
  Debt D19, D13, D5, D8, D2, D20.
- **Source:** `docs/spec-payments-week5.md`, `docs/scoping-payments-week5.md`.

---

## Context

Dana asks for a self-serve payment form so borrowers can pay by card and ACH, describes the
"I was charged twice" tickets as customer confusion, and asks us to keep it simple.

The tickets are not confusion. `scripts/repro_double_charge.py` drives the real
`POST /payments` path against the running stack and turns one $100.00 payment intent into
**eight `payments` rows totalling $800.00, every attempt returning `200`**. The same run
credits only **$600.00** to the loan. Two defects produce that result and they have separate
causes:

- **No idempotency.** Nothing in the request identifies the customer's intent as distinct
  from the HTTP attempt. `payment-service/app/payments.py:74` states the position outright:
  *"No idempotency check. No unique charge reference. Every POST inserts a row."* The DDL
  agrees (`db/init/001_schema.sql:121`).
- **Lost updates.** `balance.apply_payment` performs an unlocked read-modify-write
  (`servicing-service/app/main.py:80-85`). Concurrent applies read the same opening balance
  and overwrite one another, so captured money never reaches the loan. With no ledger (D2),
  nothing can attribute the gap to a customer afterwards.

Both compound against the borrower at once: charged more, credited less.

Two further facts constrain the design.

**The charge handler exists twice.** ADR 0004 copied it into `payment-service` and left the
original routed in `servicing-service`. `payment-service/app/payments.py:76` and
`servicing-service/app/payments.py:75` both insert into the same table, and both carry the
same "no idempotency check" comment. Any dedupe implemented in one service leaves the other
writing unguarded rows.

**The prototype retains prohibited data.** `payments.cvv` holds sensitive authentication
data, which may not be retained after authorization under any condition. `payments.pan`
holds the card number in plaintext. Both have applied to every payment since go-live. ADR
0003 accepted this on the grounds that *"disk is encrypted and we are behind the VPC"* and
recorded the consequence as *"none material"*. That reasoning does not survive contact with
the threat it needs to address: volume encryption protects a stolen disk, not a SQL
injection, a compromised application credential, a replica, a backup, or an ordinary
`SELECT`. ADR 0003 also carries an in-house reviewer note from 2025-01 — *"Are we sure about
storing CVV?"* — which was never resolved. This ADR resolves it.

A self-serve form changes who initiates a money movement, from a Meridian employee to a
member of the public. It is therefore also the first time a browser sits in the payment
path — which is the only moment at which the cardholder data footprint can be made smaller
rather than larger.

---

## Decision

### Decision 1 — Idempotency is enforced by a database constraint, not by application code

We will require a client-minted `Idempotency-Key` header on `POST /payments`, store it with
a fingerprint of the request, and enforce uniqueness with a partial unique index on
`payments`. The fingerprint covers the **rail-specific instrument** — `(loan_id,
amount_minor, method, card_token, bank_token)` — so a reused key against a different bank
account (ACH) or card returns `422`, not a false replay; a fingerprint over `card_token`
alone would be blind to the ACH instrument. The `processor_idempotency_key` forwarded to the
processor carries its **own** partial unique index on `payments` for the same reason the
Meridian key does: the design's premise is that money invariants live in the schema because
multiple writers exist, so the "the processor enforces its own uniqueness" delegation is not
enough — a drifted generator or manual SQL stamping one processor key onto two rows would let
the processor collapse the second charge while Meridian still applies its second row, the
version-skew bug this decision exists to prevent. The local index fails that duplicate insert
before any processor call. The write is insert-first — `INSERT ... ON CONFLICT (idempotency_key)
WHERE idempotency_key IS NOT NULL DO NOTHING RETURNING id` — and **the key is claimed before
the processor is contacted**, so two concurrent identical requests cannot both reach the
processor. The conflict target must carry the index predicate: the arbiter is a *partial*
unique index, so a bare `ON CONFLICT (idempotency_key)` matches no arbiter and Postgres raises
`there is no unique or exclusion constraint matching the ON CONFLICT specification` at runtime,
failing the insert before it claims the key. This is the single copyable pattern; the R-DDL
vector (`docs/spec-payments-week5.md`) runs it against real Postgres. A replay returns the original
status and body with an `Idempotent-Replay: true` header; a reused key with a different
fingerprint returns `422`; an in-flight duplicate returns `409` with `Retry-After`.

We will also make the balance mutation atomic, and derive what it moves from the payment
rather than from the request: one `UPDATE balances SET balance = balance - :amount` where the
amount is the one recorded on the referenced payment, committed in the same transaction as an
append-only `payment_applications`
record that is unique on `payment_id` and is itself written by `INSERT ... SELECT` over that
row. The caller supplies a `payment_id` and nothing else that a write reads — the loan
credited and the amount credited both come out of the row, and either statement affecting no
row rolls the transaction back, so an append-only record never outlives a movement that did
not happen. This is a separate fix from the idempotency key and neither substitutes for the
other.

#### Options considered

| Option | Why rejected |
|---|---|
| **A. Application-level dedupe** — check for an existing charge before inserting | Rejected on two grounds. It is a read-check-then-insert, so two concurrent requests both pass the check — precisely the case the reproduction exercises. And it would have to be implemented identically in two services that already drifted once; a support engineer running SQL is a third writer it cannot bind. |
| **B. Distributed lock in Redis** keyed on the request | Rejected. It adds a failure mode (lock service unavailable) to a money path that currently has none, and it must still be paired with a database constraint to be correct — so it is the constraint plus an extra dependency. Redis is already in the stack for sessions; borrowing it for correctness rather than for caching changes the effect of a Redis outage from "users are logged out" to "no payment can be taken". |
| **C. Unique constraint on `(loan_id, amount, created_at::date)`** — dedupe on the natural key | Rejected. It refuses a genuine second payment of the same amount on the same day, which is a real customer behaviour, and it silently changes product semantics to buy a schema shortcut. |
| **D. Chosen: partial unique index on a client-minted key** | Binds every writer including unknown ones; arbitrates the concurrent case at the only layer that can; nullable so pre-migration rows remain valid. |

### Decision 2 — The PAN is tokenized and sensitive authentication data is deleted

We will remove `payments.cvv` and `payments.pan` from the schema, purge the existing values,
and rewrite the table so the bytes are physically gone. We will retain only `card_token`,
`card_brand`, `card_last4`, expiry, and `bank_token`. The browser collects the card in
processor-hosted fields and exchanges it for a token client-side, so the PAN never reaches
a Meridian server. The API refuses any request carrying a `pan` or `cvv` field and fails
closed on unknown fields. A blocking CI job, `no-sad-gate`, asserts all of this the way
`redactor-drift` and `secret-scan` already hold their controls.

ADR 0003's requirements are met rather than dropped:

| ADR 0003 requirement | Answered by |
|---|---|
| Support "sees the card on file" when a borrower calls | brand + last-4 + expiry, which is what caller verification actually uses |
| Finance re-runs a charge without re-asking | a card-on-file charge against the stored processor token |
| *(implicit)* the CVV is needed to re-charge | Incorrect. Card-on-file transactions do not use the CVV, so retaining it purchased nothing even on ADR 0003's own terms. |

#### Options considered

| Option | Why rejected |
|---|---|
| **A. Keep the PAN, add column-level encryption** | Rejected. It leaves the data in Meridian's assessment scope and adds key management, key rotation, and a decryption path that application code must hold. It also does nothing about the CVV, whose retention is prohibited outright rather than conditionally. |
| **B. Stop writing the columns, leave them in place** | Rejected. Historical values remain readable, so the violation continues. A column nothing writes is also a column someone re-uses. |
| **C. Server-side tokenization** — Meridian receives the PAN and exchanges it for a token | Rejected as the primary design, though it is the fallback if Decision 3's processor lacks hosted fields. The PAN transits and is briefly held by Meridian systems, so the servers stay in scope; it is strictly worse than not receiving it, for the same integration effort. |
| **D. Chosen: browser-side tokenization, columns dropped and purged** | The PAN never reaches Meridian, the prohibited data is gone rather than hidden, and the assessment footprint shrinks at the moment the form makes that possible. |

### Decision 3 — Self-serve access uses a capability token, not borrower accounts

We will extend the ADR 0010 Phase B continuation-token pattern to servicing as a
`pay:loan:{id}` capability: scoped to one loan and one verb, stored as a keyed hash, bounded
by an expiry, and never exposed to the browser — the BFF gateway exchanges the raw token for
an opaque session id delivered in an HttpOnly, SameSite=Strict, path-scoped cookie, exactly
as it already does for applications.

Three rules are additions rather than inheritance, each answering a specific failure:
`GET` is non-consuming (corporate mail scanners fetch every emailed link before the
recipient does); one knowledge factor is required at the landing page before any figure is
shown (the link alone is a bearer credential and forwarded mail is routine); and a
`payment_token_version` column allows bulk revocation of a whole statement batch.

The capability token and the idempotency key stay separate. The token answers *may this
caller pay this loan* and survives repeated attempts; the key answers *is this the same
payment intent* and expires in hours. Merging them makes a declined card kill the link.

Launch is phased: staff-assisted payment (existing logins) ships with Decisions 1 and 2 and
needs none of this; pay-by-link follows once a delivery channel exists; enrollment at
sanction is the target end state and is out of scope here.

#### Options considered

| Option | Why rejected |
|---|---|
| **A. Borrower enrollment now** (register, password, session) | Rejected for this phase, not on principle. It does not escape the dependency it appears to solve — password reset needs the same verified channel pay-by-link needs — and it adds a credential store on top of `users.password_hash`, which is unsalted sha256 (`gateway/app/auth.py:31`). It also makes the D8 RBAC work a hard prerequisite, because a session is ambient authority over everything the role can do while a capability is not. |
| **B. Extend the existing session model to borrowers with no new authorization** | Rejected. `GET /lss/loans` returns the entire portfolio to any caller today and `frontend/app/my-loan/page.tsx:50-53` filters it in the browser. Issuing borrower sessions against that turns a latent exposure into a live one for the whole book. |
| **C. Officer-only payment taking, no self-serve at all** | Rejected as an end state, adopted as phase 1. It does not deliver what Dana asked for, but it is the correct first phase because it needs no new access model and lets the two defects be fixed immediately. |
| **D. Chosen: capability token, phased** | Ownership becomes an invariant established at mint time rather than a lookup per request; there is no serial id to walk; and the pattern is already built and hardened in this repository. |

---

## Consequences

### Positive

- One customer intent produces one charge, enforced at the layer that binds every writer.
- Captured money reaches the loan. The measured $200 loss per 8-way run goes to zero.
- Meridian stops retaining prohibited data, and the browser-side collection model shrinks
  the assessment footprint instead of growing it.
- The money-creation path through `apply-payment` closes.
- `payment_applications` is the ledger seam: applications become events, so a future ledger
  reads this table rather than re-modelling.
- Self-serve arrives without an identity programme, a credential store, or the RBAC work.

### Negative / tradeoff (accepted)

- **Clients must mint and resend a key.** A caller that does not is refused. This is a
  breaking API change, mitigated by there being no external consumer today.
- **Pay-by-link trains borrowers on the phishing pattern** — a mailed link that opens a
  payment page with no login. There is no control that fixes this. It is accepted, and it
  is the substantive argument for enrollment at sanction as the end state.
- **Statement generation becomes credential issuance.** A batch defect that mails one
  borrower's link to another hands out a live capability, so that job moves into the
  security review boundary and costs more to change.
- **Historical cardholder data outlives this ADR.** The purge covers the live table. WAL
  segments, replicas, and existing backups need their own retention action, owned elsewhere.
- **Two charge handlers remain.** The unique index covers the correctness risk; the
  duplication stays as maintenance cost and is logged as new debt.
- **`balances` stays a mutable float column.** Only its mutation becomes atomic. D2 is
  narrowed, not closed.

### Neutral

- `payments` grows six columns and loses two. New money columns use integer minor units per
  ADR 0012; the existing float `amount` is untouched.
- One new table, `payment_applications`, append-only by the same trigger pattern as
  `decision_events`.

---

## Cross-cutting concerns

**Security.** Prohibited data is deleted rather than protected. The API fails closed on
unknown fields, so a caller still sending a PAN is refused rather than silently accepted.
The capability token is hashed at rest, bounded by expiry, revocable in bulk, and never
reaches JavaScript. `apply-payment` becomes internal-only. Redaction gates stay green; this
design adds no new PII to any store or log.

**Performance.** The insert-first pattern replaces a read plus an insert with a single
statement, so the common path gets shorter. The atomic `UPDATE` serializes concurrent applies
on one row lock — correct, and a contention point only for many simultaneous payments
against a single loan, which is not a real access pattern. The partial unique index adds one
index maintenance cost per insert.

**Scalability.** No new service and no new datastore. Idempotency keys are rows, not
in-memory state, so the design does not assume a single application instance — which the
current one implicitly would if dedupe lived in the process.

**Reliability.** A crash between claiming the key and recording the outcome leaves a
`processing` row; a reaper resolves it by querying the processor with the same key. A failed
apply is now visible as the absence of an application record rather than being reported as
success. Both are improvements on today, where a servicing timeout yields a captured charge,
an unmoved balance, and a `200`.

**Maintainability.** Correctness lives in the schema, so a future service that writes
`payments` inherits it without knowing this ADR exists. The capability token reuses an
existing pattern rather than introducing a second one.

**Cost.** Processor tokenization is typically included in card processing. The measurable
saving is assessment scope: browser-side collection keeps Meridian servers out of the
cardholder data environment, which is the largest recurring compliance cost in this design.

**Operational impact.** The purge migration requires a table rewrite (`VACUUM FULL`, or
`pg_repack` where downtime is unacceptable) — the step most likely to be skipped, and the
one that decides whether the migration is a remediation or a comment. Statement generation
gains a security review obligation. `no-sad-gate` joins the blocking CI set.

**Testing impact.** Idempotency and apply-dedupe vectors run outside the tolerated
`|| true` backend matrix. The Week 4 precedent applies directly: the actuarial APR defect
survived because the money test that would have caught it ran under `|| true`. Concurrency
vectors require parallel connections against a live stack, and the purge vector requires a
populated pre-migration volume — neither is observable from a repository checkout.

---

## Implementation plan

1. Migration `0016` — idempotency columns and the partial unique index. Backward compatible;
   legacy rows keep a `NULL` key.
2. Migration `0017` — `payment_applications` plus its append-only trigger. Atomic apply and
   the `X-Internal-Service` gate land with it.
3. Idempotency contract in `payment-service`, and the same enforcement path in
   `servicing-service`'s copy until it is retired.
4. Migration `0018` — purge, drop `pan` and `cvv`, add the token columns, rewrite the table.
   Seed scrub and docstring corrections in the same change.
5. `no-sad-gate` in CI, blocking, before the form is written.
6. Migration `0019` and the capability token — phase 2 only, gated on the delivery channel.
7. Frontend: processor-hosted fields, then the payment form.

Steps 1–5 deliver phase 1 (staff-assisted) and fix both measured defects. Step 6 onward is
phase 2 and can slip without holding them.

---

## Rollback strategy

Steps 1–3 are additive and reversible: drop the index and the columns, and behaviour returns
to today's. `payment_applications` can be left in place harmlessly.

**Step 4 is not reversible, by design.** Once the PAN and CVV are purged and the table
rewritten, the values are gone. That is the point. The rollback for a defect in step 4 is to
fix forward, and it is why the token columns are added and populated before the old ones are
dropped, in a separate migration from the drop. Any tokenization defect surfaces while the
PAN is still available.

Step 6 is revocable rather than reversible: bump `payment_token_version` across the book and
every outstanding link dies.

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| The purge migration runs without a table rewrite, so cardholder data stays in the heap and the change is cosmetic | A test vector inspects a populated pre-migration volume after the migration and fails if the PAN byte sequence is recoverable |
| The idempotency key is added to one service and not the other | The constraint is in the schema, so the second writer is bound whether or not it is updated |
| The frontend mints a key per HTTP attempt rather than per form submission, defeating the design | Stated explicitly in the spec; a vector asserts that a retried submission reuses the key |
| A processor without hosted fields forces server-side tokenization | Fallback is Decision 2 option C; the PAN transits but is not stored, and only the assessment scope changes. Confirmed by client question Q1 before build |
| The delivery channel never arrives, stranding phase 2 | Phase 1 ships without it and serves the whole book. Sequencing is deliberate, not optimistic |
| A statement batch defect mails links to the wrong borrowers | Bulk revocation via `payment_token_version`; the batch enters the security review boundary |
| Concurrent payments on one loan contend on the row lock | Accepted. Many simultaneous payments against a single loan is not a real access pattern, and correctness outranks throughput on a money path |

---

## Assumptions challenged

- **"The complaints are customer confusion."** False, and measured. Eight charges from one
  intent, all reported successful.
- **"Disk encryption makes stored card data safe"** (ADR 0003). It addresses a stolen disk
  and nothing else in the actual threat set — injection, a compromised credential, a
  replica, a backup, or a routine query. It has never applied to the CVV, whose retention is
  prohibited outright.
- **"The CVV is needed to re-charge."** Card-on-file transactions do not use it.
- **"Self-serve requires borrower accounts."** It requires an authorization decision, not an
  identity system. The capability token makes ownership an invariant of the credential.
- **"An idempotency key fixes the double-charge."** It fixes half of it. The lost update is a
  separate defect with a separate fix, and the reproduction is what distinguished them.
- **"Keep it simple."** Honoured literally. Not storing the PAN is simpler than storing it;
  one constraint is simpler than dedupe in two services; a capability is simpler than
  accounts. The complexity worth resisting — the ledger, the identity programme, ACH return
  automation — is deferred by name.

---

## Sign-off status

**Proposed.** Engineering position recorded here; product and compliance review outstanding.
Eight client questions are open in `docs/scoping-payments-week5.md` §6, with assumptions
recorded so a different answer costs a configuration change. Only Q7 (delivery channel)
affects sequencing, and phase 1 is deliberately independent of it.

Decision 2 carries a remediation obligation that does not wait for this ADR to be accepted:
the CVV is being retained today, for every payment since go-live.
