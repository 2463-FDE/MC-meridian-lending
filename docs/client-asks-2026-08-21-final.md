# Client asks — 2026-08-21, the final set

**Audience:** Dana (VP Lending Ops, Meridian) · **Written:** 2026-08-21 ·
**Cycle:** code freeze **Wednesday 2026-09-02** (moved from Friday 2026-08-28 by the 2026-08-21 program email), handoff Friday 2026-09-04 · **Status: DRAFT, unsent.**

**Assumption this document is built on: this is the last cycle.** That changes the test for
including a question. Until now the test was "does this block work before freeze"; anything else
was parked to the next cycle. There is no next cycle to park to, so the test becomes **"will anyone
ever ask this again."** Section 4 exists only because of that, and it is the section that would
have been deferred under the old test.

**Read first:** `docs/client-asks-2026-08-17-asked-vs-mapped.md` — what in her 08-17 reply is her
instruction and what is our inference. Items 3 and 4 below come straight out of it.

---

## What this does NOT ask, and why that matters

**Nothing already in her hands is re-asked.** Her 08-17 reply deferred four items to 2026-08-28 by
her own choice and left two more open on her own authority. Re-asking any of them reverses her
decision and spends a question we do not have to spend.

| Not asked here | Because |
|---|---|
| Export source · policy-corpus currency · who determines a Refer · print-and-mail vendor | Sent 2026-08-16 as items 5, 6, 7, 8. **She deferred all four to 2026-08-28.** Section 2 |
| Which processor · which PCI scope | *"I am not naming a processor or a compliance scope in this note, and neither is needed for the fix to proceed."* Declining is the answer |
| Whether the three double-charge customers were made whole | *"I would rather leave that open than hand you an instruction I cannot stand behind."* Open on her authority. Section 3 |
| Purge owner and target date | *"leave the owner and target date open until I can give you both properly."* She will supply them. Section 3 |
| Retiring the second charge path | She refused it, and replaced it with a stronger requirement. Settled |
| The replay window value | Answered: configurable, 24 hours. Settled |
| Whether to forward a separate processor key | Answered, unprompted, and it was already the design. Settled |
| Whether to keep security codes | Answered, and it is the firmest sentence in the reply. Settled |
| The DTI and pricing disagreements | Answered: both real, both product direction, keep quoting the documents |

---

## 1. Before freeze — four questions

Two that never reached her, and two her own 08-17 reply raised.

**Two of the four no longer gate the freeze, and the send should say so rather than imply urgency
it no longer has.** The 2026-08-21 program email made the agentic design and trace 100% of the
delivery priority and displaced the payment-integrity work (D19 duplicate charge, D3 lost update)
off the critical path. Q3 and Q4 are both payment-integrity questions, so neither blocks
2026-09-02. They stay in the send because this is the last cycle and nobody asks them afterwards —
not because the freeze turns on them. Q1 and Q2 are unchanged: neither ever depended on the freeze
date.

### Q1 — Who receives the reconciliation alert, at what time of day, and through what channel?

**Never asked.** Not deferred, not answered — lost in a split. The 08-12 draft's row 6 held two
questions; the cut-off half became ask 2 and came back answered on 08-14, so the row read as
settled while the recipient half sat in no table at all.

**What it blocks:** nothing in code, everything in effect. The control fires on the 500 minor units
she set and exits so a cron can escalate, but the runbook's cron example mails `ops@example.com`, a
placeholder — docs/runbook.md:168 **on `main`**, whose line 208 also states outright that no
recipient is configured. Un-backticked and branch-qualified because this branch carries an older
135-line runbook in which those lines do not exist. It detects correctly and reports to nobody.

**Stated fallback if she has no preference:** email to a shared Finance Ops mailbox — what the cron
example already does, and it needs nothing built.

**Consequence of no answer:** the cron ships commented out rather than wired to a placeholder, and
the check stays something an operator runs by hand.

### Q2 — When reconciliation finds a **duplicate** charge, is the borrower told, is a refund submitted on their behalf, and who owns that refund?

**Never asked** — raised 2026-08-20, the day after her reply. Asked as one question deliberately;
split into three it invites three separate deferrals.

**What she has already settled, and this ask concedes rather than reopens:** no notification until a
verified contact channel exists (week-6 Q9); no remediation or ticketing workflow, and loan 4471
stays an open exception for Finance Ops (08-14, reaffirmed 08-17).

**Why that does not close it:** both of those concern `MISSING_IN_LEDGER` — money captured and never
credited. A duplicate capture is a **different break class**, reported separately and excluded from
the variance figures because the money is already counted on whichever side it landed.

**Today's position, verified against `main` rather than assumed:** the duplicate is detected as a
pair, including across a window edge. Repeated capture is not prevented. The borrower is not
notified and **no outbound sender exists in any service** — verified 2026-08-21 by searching every
service for `smtplib`, `sendmail`, SendGrid, Mailgun and Twilio: no match, and the only `boto3`
usage is the Bedrock LLM adapter. No refund, void or reversal path exists
and the processor client exposes none. Refund status is visible nowhere.

**Consequence of no answer:** prevention still ships — nobody has to approve not double-charging
people. A borrower charged twice before it ships is told by nobody and finds out on a statement.

### Q3 — Confirm: the duplicate-detection fingerprint must not include the card security code, in any form — including a hash. Is that your intent?

**This is our reading of her rule, not her instruction.** She was firm that security codes may be
collected to authorise and not kept afterwards. To recognise a retry we store a fingerprint of the
request; if the security code were part of it, we would be keeping a value derived from something we
may not retain, and every retry where the customer re-enters the card without the code would fail to
match.

**One line is enough.** The exclusion is **specified, not yet built** — `docs/spec-payments-week5.md`
defines the fingerprint as `sha256` over `(loan_id, amount_minor, method, card_token, bank_token)`,
which carries no security code in any form. On `feat/payment-idempotency-week9` today
`request_fingerprint` is a column with no writer, so there is nothing computing a fingerprint to
exclude anything from. Say "specified" to her, never "built". The audit doc records the exclusion as
our inference rather than her decision until she confirms.

### Q4 — On the older charge path, is a second identical charge on the same loan within the same day ever a legitimate payment rather than a duplicate?

**Comes directly out of her own instruction.** She asked us not to retire that path and to *"enforce
the uniqueness rule in the database so every writer is bound the same way regardless of which route
it came in on."*

**Why that turns into a question.** The newer path is bound by a key the caller supplies, so a
customer making a genuine second payment sends a fresh key and it goes through. The older path
sends no key. To bind it we derive one from the request itself — which means two identical charges
on that route inside the window look identical to us, and the second is refused.

**What is true of the schema today, and why it makes this question load-bearing.** Both unique
indexes on `payments` are partial — `WHERE idempotency_key IS NOT NULL` and
`WHERE processor_idempotency_key IS NOT NULL`. Postgres treats every NULL as distinct, so a row
written by the keyless route matches neither index and **the older path is not bound at all today**.
`request_fingerprint` cannot substitute: it is an unindexed column, so a derived value stored there
arbitrates nothing. Binding that route means writing the derived value into `idempotency_key`
itself, under the index that already exists — which is exactly the decision her answer settles. Her
instruction, *"every writer is bound the same way regardless of which route it came in on"*, is
therefore **not yet satisfied by the shipped rung**, and reading it the weak way (a database
constraint rather than application logic) would let us call it done while the second handler keeps
double-charging. See `docs/client-answers-2026-08-17-payment-integrity.md` (G3) for the
strong-versus-weak reading.

**If the answer is "never legitimate,"** binding it is strictly correct and cheap.
**If it is "occasionally legitimate,"** we need a narrower rule and the work grows.

**Consequence of no answer:** we build the strict version, which is the safer direction — a refused
legitimate payment is visible and retryable, a duplicate charge is neither. The decision is recorded
as ours.

---

## 2. Already asked, deferred by her to 2026-08-28 — no action, listed so nothing is lost

Sent 2026-08-16, deferred in her 08-17 reply: *"The rest of your list can wait for the 28th as
planned."*

| Item | Question | What waits on it |
|---|---|---|
| 5 | What is the source of the back-office export — screen, report, or direct database extract? | Her own 08-13 instruction, *"the fix that matters is making the export carry all four, and that I want this cycle"*, is half-satisfied: the screens now carry all four reasons, and we cannot verify the export does without knowing what it is |
| 6 | Are the guidelines and fee schedule current, what else should be indexed, and how do updates reach us? | Whether officers are quoted a current document. Both internal disagreements inside this item are already answered |
| 7 | When a decision returns *Refer*, who makes the final determination and where is it recorded? | Coverage of the monitoring report — referrals leave no captured outcome |
| 8 | Print-and-mail vendor, its intake contract, and who owns the spreadsheet timing process | Card G1's estimate cannot close without all three. Until it lands, **delivery of an adverse-action notice cannot be proven** |

## 3. Open on her authority — track, do not chase

| Item | Her position |
|---|---|
| Purge owner and target date for stored card numbers and security codes | *"leave the owner and target date open until I can give you both properly"* |
| Were the three double-charge customers made whole | *"I do not have a recorded answer … I would rather leave that open."* No list, no corrections, no workflow |
| Sam's walkthrough of the corrected audit-trail statements | *"I will come back to you"* — does not block |
| The ops-manual reason-code list (was due 2026-08-14) | *"I will come back to you"*; free text plus the existing category is the right stopgap. **We have no record it arrived** — worth one line of confirmation |

## 4. Never asked, and there is no next cycle to ask in

None of these blocks anything before freeze. All of them are things only she can answer, and after
handover nobody will be here to ask. **Send with the 08-28 batch, not now** — they need room, and
Q1–Q4 must not queue behind them.

### Disclosure and pricing — never asked, and the exposure is on loans already written

**Q5.** Confirm the actuarial method as the APR of record. We cannot reproduce the worked APR figure
in the original brief using the actuarial method; we reproduce it exactly using add-on
annualisation, which is what the previous code did. On the products we tested the two differ by
roughly **4.5 percentage points**, well outside the disclosure tolerance. If that figure came from a
rate sheet, a vendor calculation or an examiner's own working, point us at it and we will re-run our
solve against it.

**Q6.** Which tolerance applies to this product — the regular-transaction 0.125 percentage points
we are enforcing, or the irregular-transaction 0.25?

**Q7.** Did the earlier APR figure reach borrowers, and do those disclosures need curing? Our fix
applies forward only; disclosures already delivered are never recomputed.

**Q8.** The ops manual sets a $150 monthly fee-waiver guideline. Is that window a **calendar month
or a rolling 30 days**? Never asked; we assumed calendar month, and the assumption is unlabelled in
the card that carries it.

### Handover — who holds each of these after we go

**Q9.** Who runs the daily reconciliation close, and who escalates a close that fails? (Distinct
from Q1: that is who receives the alert, this is who operates the job.)

**Q10.** Adjustment history is retained seven years, confirmed by Sam. **What happens at year
eight?** Nothing is designed and nobody has asked — the ledger satisfies seven years by never
deleting anything.

**Q11.** Who owns each thing that is specified and not built: the lost-update fix, the purge and
tokenisation of stored card data, second-person approval on discretionary balance moves, the
adverse-action notice channel, drafted notice wording, the payment waterfall, a real ledger, the
balance reconciliation that card C3 holds, PCI scope, and processor selection?

**Q12.** Who accepts the residual risk register, in writing? Handed over unfixed: card numbers and
security codes at rest until the purge runs, hardcoded credentials, money as float with a single
mutable balance column and no ledger, an `audit_logs` table that is mutable while the README claims
full SOX audit, Postgres and Redis published on the host in the base compose file, and servicing
authorisation without second-person approval or role enforcement at the gateway.

---

## 5. Notices, not questions

Neither needs an answer. Both need saying, and the second is her own instruction.

- **A retried payment stops charging again.** Today each submission charges; after this cycle a
  repeat inside the window returns the first payment. That is a behaviour change on a live money
  path and CSRs will see it as new.
- **The balance-correctness work slips past freeze.** She named it by hand as something not to be
  displaced, and asked that anything bigger than it looks *"becomes a card and you tell me — don't
  absorb it."* This is that. Payment idempotency goes first because it stops new double charges;
  the lost-update fix is designed, has a runnable failing test as its before-number, and is written
  up for whoever picks it up.

---

## Handoff copy — BLOCKERS ONLY, send now

_The email body for this section is held outside this repository with the other client-facing copies. This log keeps the reasoning, the decisions and her replies; the sent text is not published here._

## Handoff copy — the fuller version, questions 1 to 4

_The email body for this section is held outside this repository with the other client-facing copies. This log keeps the reasoning, the decisions and her replies; the sent text is not published here._
