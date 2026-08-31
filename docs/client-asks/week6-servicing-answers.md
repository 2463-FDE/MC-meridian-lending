# Client answers — week-6 servicing money controls

**Asked:** week-6 servicing questions to Lending Ops · **Answered:** 2026-08-12, in writing
**Respondent:** Dana Whitfield, VP Lending Ops, with Sam (control owner) on retention
**Consumes these answers:** `adr/0014-servicing-money-controls.md`,
`docs/cards-week6-servicing.md`

This file exists so the decisions in ADR 0014 can be checked against their source without the
email thread. ADR 0014 cites these answers in ten places and previously described them only in
passing; a reviewer had no way to see what was actually asked, what the client actually said,
and which parts nobody asked. Answers are transcribed, not paraphrased into the decision that
uses them — where the ADR goes further than the answer, the gap is named below.

Each answer records the decision it feeds, so a changed answer points at the decision that has
to be revisited.

---

## Q1 — Who may move money, and who may read a serviced loan?

**Answer.** CSRs and admins move money. Underwriters and borrowers do not. A borrower reads
their own account and nothing else. Underwriters may read any serviced loan — looking at a
loan is ordinary work for them.

**And: no supervisor role is to be invented.** None exists today, and creating one is work the
client would rather spend elsewhere.

**Feeds.** ADR 0014 Decision 1 — `_MONEY_ROLES = {"csr", "admin"}`,
`_STAFF_ROLES = {"csr", "underwriter", "admin"}`, reads broader than writes. The no-supervisor
answer is why maker and checker draw from one set when approval arrives (Decision 2).

**Note.** This answer is the reason servicing does not reuse origination's
`_OFFICER_ROLES = {"underwriter", "admin"}`: that set excludes the CSR, who is the operator
this work is for.

---

## Q2 — Are the servicing endpoints reachable from outside your network?

**Answer.** Yes. The borrower portal sits behind the same gateway as the internal application,
so a borrower login is on the public internet.

**Feeds.** ADR 0014 Context, and Implementation plan step 1. The answer does not change the
finding — any authenticated caller could already move money on any loan — it changes the risk
class from internal misuse to internet-facing, which is why the client asked for the
authorization fix as its own immediate change rather than as part of the dashboard.

---

## Q3 — Should a second person approve a discretionary balance move before it lands?

**Answer.** Not this cycle. Record every move now; approval comes next cycle. The client's
reasoning, with the volumes below in hand: a mandatory second approver slows the floor before
anything has been measured, and the pending state, queue, and break-glass path are a build of
their own.

**Feeds.** ADR 0014 Decision 2 (record now, approve next cycle) and card C1.

**Where the ADR goes further than the answer.** The client deferred the workflow; the ADR
additionally *fixes its design* now — any other CSR or admin approves, never the same person,
compared on `users.id` rather than role, and break-glass is permitted, recorded, and reviewed
after the fact. That design is engineering's, taken so `balance_postings.approved_by` can ship
`NULL` from the first migration and the later build adds a write path rather than a schema
change. It is not a client answer and is open to review on its merits.

---

## Q4 — How many discretionary moves happen, and how many people could approve?

**Answer.** About **30 balance adjustments and 15 fee waivers a week**, across **9
representatives**, with **3 people who could approve**.

**Feeds.** The deferral in Q3 rests on this ratio — 45 discretionary moves a week against 3
approvers. It appears in ADR 0014 Decision 2, its option-A rejection, the Operational impact
section, and card C1.

**Note.** These are the client's operating figures, not measurements taken from the database.
Nothing in the schema records who made an adjustment today, so the platform cannot corroborate
them — that gap is the finding the ledger closes.

---

## Q5 — What reason must a representative give, and is there a code list?

**Answer.** A reason is required on every manual move. The ops-manual reason codes are being
sent **Friday 2026-08-14** — after this ADR is written.

**Feeds.** ADR 0014 Decision 3. Until the list arrives, `reason_code = 'other'` with free-text
`reason_text` is the only value; `'other'` stays permanently available once the list exists, so
the column is a list plus an escape hatch and never a closed enum. A representative who cannot
find their case must not be forced into a wrong code to complete a correction.

**Still outstanding as of 2026-08-12.** The list itself. Its arrival changes data, not schema.

---

## Q6 — What does a controller need to see on an adjustment record?

**Answer.** The figure before and the figure after, on the record itself.

**Feeds.** ADR 0014 Decision 3 — `before_minor` and `after_minor` are stored rather than
derived. A sum can be recomputed, but a reconstruction that depends on replaying every prior
posting is not the artifact a controller reads a row for.

---

## Q7 — How long must adjustment history be retained?

**Answer.** **Seven years.** Confirmed by Sam as the control owner, not by Lending Ops.

**Feeds.** ADR 0014 Operational impact. No retention or archival mechanism is designed in this
ADR — the ledger is append-only and grows unbounded, which satisfies seven years by not
deleting anything. A retention *policy* (what happens at year eight) is not decided and nobody
has asked for one.

---

## Q8 — Should balances be reconciled against payment records before the ledger starts?

**Answer.** No. The client's words: an honest line in the sand beats a reconstructed history
that cannot be defended.

**Feeds.** ADR 0014 Decision 3 — the ledger opens with one `entry_type = 'opening'` posting per
account carrying today's stored balance, labelled as the opening figure precisely because that
figure may be wrong. Card C3 holds the reconciliation as a separate, unscheduled project.

**Context the client had when answering.** The stored balance may be wrong: the D3 concurrency
defect measured $800.00 captured against $600.00 credited over eight concurrent applies on
2026-08-02, and it is the same defect behind the three double-charge tickets.

---

## Q9 — Should the borrower be told when a representative adjusts their balance?

**Answer.** No notification for now; record only. Take it together with the delivery-channel
decision from the payments work — both need a verified way to reach the borrower, and the
platform has no such channel today.

**Feeds.** Card C2, which is explicitly not estimable until the channel exists.

---

## Q10 — Is the $150 monthly fee-waiver guideline a limit the system should enforce?

**Answer.** The ops manual sets $150 per account per month with escalation above it. The system
has never enforced it, and this work does not start.

**Feeds.** ADR 0014 Decision 3 — the dashboard shows the representative the limit and the
month's total to date, and the reason is captured either way. Card C4 holds enforcement, folded
into C1 because "escalate above $150" needs an approver to escalate to.

**Untested assumption, flagged in C4.** Whether the window is calendar month or rolling 30 days
was not asked. C4 assumes calendar month.

---

## Q11 — What is in scope this cycle?

**Answer.** The comprehension work — the report, the characterization tests, the failing
lost-update test, and this decision record. The dashboard is the original ask and waits. The
client set that scope explicitly, and asked that the deferred pieces be **carded** so they are
planned rather than dropped.

**Feeds.** `docs/cards-week6-servicing.md` exists because of this answer. Cards C1 through C5
are the five deferrals.

---

## Not asked, and therefore not a client answer

- **How money is represented in storage** (ADR 0014 Decision 4 — integer minor units in the
  ledger, float projection). Never put to Lending Ops: representation is not a business
  question. The client's interest is that the figures are right. Nothing in Decision 4 changes
  if a client answer arrives.
- **The approval workflow's internals** — see Q3 above.
- **Postgres role separation**, so a representative cannot bypass the gate with raw SQL. Out of
  scope in ADR 0014; the mitigation there is operational.

## Asked earlier and still unanswered

The week-5 payments questions are still open as of 2026-08-12 and block the payments phase
rather than this one: card-data purge owner and date, whether the three double-charge customers
were made whole, the APR method of record, the tolerance regime, back-book curing, PCI scope,
the processor, and the borrower delivery channel that card C2 waits on. The draft asking them
is on the unmerged `feature/payments-week5` branch, so it is not citable as a path from here.
