# Cards — servicing money controls (deferred work)

**Raised:** 2026-08-12 · **Source:** Lending Ops' written answers to the week-6 servicing
questions · **Decision record:** `adr/0014-servicing-money-controls.md`

Five pieces of work the client deferred on purpose and asked to see carded, so they are planned
rather than dropped. Each is out of scope for the current cycle by the client's own decision, not
by omission.

Estimates are engineering days for one person, and they assume ADR 0014 steps 1–3
(authorization, ledger, posting on every mutation) have landed — every card below sits on top of
that trail. They are planning figures, not commitments.

---

## C1 — Approver workflow and break-glass

**What.** A second person approves a balance adjustment or a fee waiver before it moves. A
pending state, a queue the approver works from, and a break-glass path that lets a lone
representative proceed with the move recorded and reviewed after the fact rather than blocked.

**Design is already fixed** (ADR 0014, Decision 2), so this starts from a decision: any other CSR
or admin approves, never the same person — compared on `users.id`, not on role — and
`balance_postings.approved_by` already exists, `NULL`, from the ledger's first migration.

**Why deferred.** The client's measured volume: about 30 balance adjustments and 15 fee waivers a
week across 9 representatives, with 3 people who could approve. A mandatory second approver at
that ratio slows the floor before anything has been measured. This is the client's call, made
with the numbers in hand.

**Estimate.** 4–6 days. The queue and the pending state are most of it; the approval check itself
is small. Add 1–2 days if the dashboard has to render the queue.

**Depends on.** ADR 0014 steps 1–3. Not on C5 — the queue can be an internal view first.

**Owner.** Engineering, with Lending Ops naming who approves.

**When.** Next cycle. The client asked for this card by name and expects it there.

**Watch for.** The break-glass path is the part that goes wrong quietly: if nobody reviews the
recorded break-glass moves, it becomes the normal path. Ship it with the review, or do not ship
it.

---

## C2 — Borrower notification on an adjustment or waiver

**What.** Tell the borrower when a representative changes their balance or waives a fee —
statement line, email, or nothing.

**Why deferred.** No notification for now; record only. The client wants it taken together with
the delivery-channel decision from the payments work, since both need a verified way to reach the
borrower and the platform has no such channel today.

**Estimate.** Not estimable alone. 1 day for the trigger and the content once a delivery channel
exists; the channel itself is the real work and belongs to the payments phase.

**Depends on.** The payments delivery-channel decision — still unanswered in the week-5 client
email (question 3(c); that draft lives on the unmerged `feature/payments-week5` branch, not on
`main`).

**Owner.** Lending Ops decides the channel; engineering builds to it.

**When.** With the payments delivery-channel decision, as one piece of work.

---

## C3 — Historical reconciliation before cutover

**What.** Reconstruct balances against payment records for the period before the ledger starts,
so the opening figure is a reconciled number rather than today's stored one.

**Why deferred.** The client's words: an honest line in the sand beats a reconstructed history
that cannot be defended. The ledger therefore opens with today's balance, labelled as the opening
figure (ADR 0014, Decision 3). The figure may be wrong — the D3 concurrency defect is the same
one behind the three double-charge tickets — and the label is what keeps that honest.

**Estimate.** 5+ days, and genuinely uncertain. It is an investigation, not a build: the
`payments` table has no idempotency key and no link to a balance delta before ADR 0013's
`payment_applications` lands, so the join that would prove a reconciliation does not exist yet
for historical rows.

**Depends on.** ADR 0013's `payment_applications` for anything forward-looking. For historical
rows, on what the processor can supply — which is a question nobody has asked yet.

**Owner.** Lending Ops owns whether it happens at all; it is a business decision about how far
back the book needs defending.

**When.** Unscheduled. Revisit if an examiner or Sam's SOX walkthrough asks for pre-cutover
history specifically.

**Note.** This is separate from making the three double-charge customers whole, which is
outstanding now and is a remediation rather than a reconciliation (week-5 client email, question
1(b), still unanswered).

---

## C4 — Waiver-limit enforcement

**What.** Enforce the ops-manual guideline of $150 per account per month on fee waivers, with
escalation above it.

**Why deferred.** The guideline exists and the system has never enforced it. ADR 0014 records and
displays it — the dashboard shows the representative the limit and the month's total to date, and
captures the reason either way — but does not gate on it. Turning an unenforced guideline into a
hard limit changes what representatives can do on the day it ships, which is a policy change and
belongs with the approval work that handles the escalation case.

**Estimate.** 2 days on its own: a rolling per-account monthly sum, the check, and the error path.
Cheaper folded into C1, because "escalate above $150" needs an approver to escalate to.

**Depends on.** C1 for the escalation path. The sum needs the ledger (ADR 0014 step 2) to be
computable at all.

**Owner.** Lending Ops confirms the figure is current and whether the window is calendar month or
rolling 30 days — this card assumes calendar month and that assumption is untested.

**When.** With C1.

---

## C5 — The servicing dashboard

**What.** The original client ask: a screen where representatives adjust balances and waive fees
without an engineer running SQL.

**Why deferred.** This cycle is deliberately the report, the characterization tests, the failing
lost-update test, and this ADR — the comprehension work the dashboard would otherwise inherit
blind. The client set that scope explicitly.

**Estimate.** 5–8 days for the screens, assuming the routes are already gated and posting. The
reason picker, the before/after display, and the month-to-date waiver total are the fiddly parts.

**Depends on.** ADR 0014 steps 1–3. Reads better after C1, since a dashboard that shows an
approval queue is a different screen from one that does not — building it before C1 means
building part of it twice.

**Owner.** Engineering, with Lending Ops on what a representative needs on screen.

**When.** After C1, on the argument above. Worth confirming with the client, because the ask
was theirs and the sequencing is ours.

---

## Not carded, still open

The week-5 payments questions remain unanswered as of 2026-08-12 and are not covered by any card
here: card-data purge owner and date, whether the three double-charge customers were made whole,
the APR method of record, the tolerance regime, back-book curing, PCI scope, and the processor.
Several of them block the payments phase rather than this one. The draft that asks them is on the
unmerged `feature/payments-week5` branch — it is not on `main`, so it is not citable as a repo
path from here.
