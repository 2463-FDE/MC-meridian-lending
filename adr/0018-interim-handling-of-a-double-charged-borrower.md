# ADR 0018: Interim Handling of a Double-Charged Borrower

- **Status:** **Proposed** — partly built as of 2026-08-23. Decision 1 (prevention at capture)
  is built: the schema rung landed on `main` via PR #63 and the capture path that claims the
  key merged via PR #65, held by the blocking `payment-idempotency-gate`. Nothing else in
  this ADR — borrower notification, refund submission, refund status — is built. This ADR
  records the decision and states today's evidence boundary. One question is open with the
  client and is named in Sign-off status.
- **Date:** 2026-08-20
- **Author:** Claude Code
- **Related:** ADR 0013 (payment idempotency and tokenization — designs the prevention this
  ADR sequences first, and is itself Proposed). ADR 0015 (settlement reconciliation as a
  control — the detection this ADR builds on). Debt D19 (`docs/debt-log.md` — the
  double-charge entry, Open, pre-existing). The client's answer of 2026-08-14 on loan 4471,
  which scopes the operator half.
- **Source:** Week-8 client-demo feedback, improvement priority 2: the interim offer to a
  double-charged borrower is undecided and unevidenced beyond detection.

## Context

Reconciliation finds a duplicate charge. It has found one: loan 5582, two `payments` rows of
41050 minor units two seconds apart. The report names both rows and reports the pair as a
signal rather than a variance, because the money is already counted on whichever side it
landed (`services/servicing-service/app/reconciliation.py:211-218`).

A borrower in that position receives nothing today. Not "receives it late" — nothing. The
platform cannot prevent the second charge, cannot tell them it happened, cannot submit a
refund, and has nowhere to show the status of one. The client asked directly what such a
borrower is offered, and the honest answer is a line in an operator's report, sent to an
address that is a placeholder.

That is half a control. Detection without a remedy identifies the borrowers owed money and
then stops, and each day the platform runs without prevention adds more of them to a set
that has no path out. The business problem is not that the detector is incomplete — the
detector works. It is that the platform's obligation to the person whose money it took twice
is undefined, so nobody can say whether it is being met.

### Today's evidence boundary

Stated plainly, because the demo must not imply more than this. Each row is verified against
`main`, not assumed.

| Question the client asked | What exists today | Evidence |
|---|---|---|
| Is a repeated capture prevented? | **Yes, for the exact-retry case.** `payments` carries `idempotency_key` under a partial unique index (PR #63), and both charge handlers claim it insert-first via `claim_or_branch()` before the processor is contacted (PR #65), held by the blocking `payment-idempotency-gate`. A processor-side duplicate or a break older than the fix is still only caught by reconciliation, not prevented. | `services/payment-service/app/payments.py:175-183,293`; `db/init/001_schema.sql:142`, `:220-221` |
| Is the borrower notified? | **No, and no channel exists to notify them through.** `applicants.email` and `applicants.phone` are unverified columns, and no outbound sender of any kind exists in any service. | `db/init/001_schema.sql:24-25`; no `smtplib`, `sendgrid`, or `twilio` anywhere under `services/` |
| Is a refund submitted? | **No.** The payment service has no refund, void, or reversal path, and the processor client exposes none. | `services/payment-service/app/payments.py` |
| Where does refund status show? | **Nowhere.** No status field, no borrower screen, no officer screen. | — |
| Is the duplicate detected? | **Yes**, as a pair, including across a window edge, and excluded from the variance figures. | `services/servicing-service/app/reconciliation.py:61`, `services/servicing-service/app/reconciliation.py:211` |
| Does an alert reach a human? | **Undetermined.** The alert fires on the client's $5.00 threshold, and the runbook's recipient is `ops@example.com`. | `docs/runbook.md` |

The client already scoped the operator half of this, on 2026-08-14, when asked who owned the
$500 on loan 4471: treat it as an open exception rather than a write-off, put both processor
references in the exception report, mark them for Finance Ops review, and — her words — *"no
separate remediation or ticketing workflow is needed in this build."* That answer binds this
ADR: it may not propose a ticketing workflow. It also does not settle this ADR, for two
reasons. It concerns `MISSING_IN_LEDGER`, where money was captured and never credited, not a
duplicate capture. And it says nothing about the borrower.

## Decision

**We will treat prevention at capture as the interim protection, and ship it before any
borrower-facing path.** Concretely:

1. **ADR 0013's idempotency key is the interim offer to future borrowers, and it ships
   first.** It is already designed — the key is claimed insert-first before any processor
   call, a same-key request carrying a different instrument returns `422`, and an in-flight
   duplicate returns `409` with `Retry-After`. We will not redesign it here; we will
   sequence it ahead of the borrower-facing work and build it in the week-9 payment
   integrity slice.
2. **We will not claim, build, or demonstrate a notification or refund path while the
   platform has neither a verified delivery channel nor a processor refund call.** A screen
   that says "refund submitted" over an operation nothing performs is worse than the absence
   it replaces, because it stops the operator from acting.
3. **We will make the detected duplicate attributable in the report** — the loan and the two
   payment ids it already carries, plus the applicant the loan belongs to — so the human who
   reads it can identify the borrower without a database query. This is the operator half,
   matching what the client authorized for the other break class.
4. **We will state the absence in the runbook and to the client, in the same words as the
   table above.** The runbook already says the alert reaches nobody until a recipient is
   set; it will also say that no borrower-facing remedy exists for a duplicate.
5. **We will hold the borrower-facing half for one question**, not three, and put it as one
   question: when reconciliation finds a duplicate, is the borrower told, is a refund
   submitted on their behalf, and who owns that refund. Her answer to that decides whether
   items 2 and 3 grow into a path or stay as they are.

## Options considered

**Option A — record the boundary, build nothing until the client answers.** Rejected. It
leaves prevention unbuilt for another cycle, and prevention is the one part of this that
needs no answer from her: she has never been asked to approve not double-charging people.
Waiting also grows the affected population while the question sits, which is the cost that
compounds. Her 08-14 reply shows she scopes work down rather than out, so a proposal that
does the cheap protective half now is more likely to match how she decides than one that
does nothing.

**Option B — build the borrower-facing path now: notify, submit a refund, show its status.**
Rejected, and not on cost grounds. It requires two things that do not exist. There is no
verified delivery channel — the same gap Week 5 Q7 records, and `applicants.email` is an
unverified column with no sender behind it. And there is no refund call: the payment service
cannot ask the processor to reverse anything. Building this means mocking both and
presenting the mock as a control, in the one area of the platform where the client's own
question was whether a control exists.

**Option C — extend the `MISSING_IN_LEDGER` treatment to duplicates and call it done.**
Rejected as sufficient on its own, adopted in part as item 3. The exception report plus a
named owner is exactly what the client authorized for the other break class, so extending it
is cheap and consistent. But it is the operator half only, it does nothing to stop the next
double charge, and the answer that authorized it was about a different break class. Adopting
it *as the decision* would let the platform report that a double charge is handled when what
is handled is the paperwork.

**Option D — build prevention at capture and the borrower path together, in one slice.**
Rejected on sequencing. The two have different blockers: prevention blocks on nothing, the
borrower path blocks on a client answer and two absent capabilities. Bundling them makes the
unblocked half wait for the blocked half, and produces a slice that cannot ship until
procurement finishes. The week-9 lost-update fix already contends for the same code path in
`balance.py`, so a smaller payment-integrity slice is also easier to review.

## Consequences

Prevention lands first, so the number of borrowers charged twice stops growing. Nothing
changes for the borrowers already in that set, and this ADR does not pretend otherwise —
their remedy waits on the open question, and the runbook says so.

The demo gains a defensible answer to the question the client asked. It is not the answer
she may want, but it is verifiable line by line, and the alternative on offer was a claim we
could not support.

The operator report identifies the borrower, so Finance Ops can act on a duplicate the same
way they act on a missing ledger row. Nothing tracks whether they did — the client declined
that, and this ADR respects the decision rather than reopening it.

Deferring the borrower path keeps a real exposure open: a borrower who is charged twice
today is told by nobody, and will discover it on a statement. That is the current state, not
a new one, and naming it is the point.

## Cross-cutting concerns

**Security.** The idempotency key is a client-supplied value that reaches logs, so it passes
the same refusal the correlation id already gets: a key shaped like a national identifier or
a card number is replaced before it is logged. Making the duplicate attributable adds an
applicant id to the report, not a name, an SSN, or a card number — the break report carries
no personal data today and must continue not to.

**Performance.** The key claim is one indexed insert on the existing payment path. Attribution
is one join the report already has the key for. Neither is a new query pattern.

**Scalability.** ADR 0013's partial unique index covers only rows carrying a key, so the
index stays proportional to live keys rather than to the payments table.

**Reliability.** Prevention fails closed: a request whose key cannot be claimed is refused
rather than charged. This is the same posture as the reconciliation threshold, which aborts
rather than reporting a clean close.

**Maintainability.** No new module and no new service. The decision reuses ADR 0013's design
rather than restating it, so there is one description of the key's behaviour.

**Cost.** No new dependency and no procurement. The deferred half is where the cost sits —
a delivery channel and a processor refund integration are both unestimated until the vendor
and the contract are named, the same shape as card G1's vendor dependency.

**Operational impact.** Finance Ops gains the borrower's identity on a duplicate. They gain
no new workflow, no queue, and no status field. The alert still reaches nobody until a
recipient is named, which is tracked separately.

**Testing impact.** Prevention needs its regression test proven red before the fix, per the
repo's own rule, and it belongs in the payment service's suite. The attribution change lands
in the reconciliation suite, which now sits behind the blocking `reconciliation-gate`, so a
regression in it cannot ship green.

## Implementation plan

1. **Attribution in the break report** — carry the applicant id alongside the existing loan
   and payment ids for a `DUPLICATE_SUSPECT` pair. Regression test in the gated
   reconciliation suite asserting the report still carries no personal data.
2. **The runbook sentence** — state that no borrower-facing remedy exists for a duplicate,
   beside the existing statement that no alert recipient is configured. Asserted by the
   runbook test, which the gate blocks.
3. **Put the one question to the client** in the next ask, alongside the alert recipient and
   the processor reference, both of which are still open.
4. **Prevention at capture**, in the week-9 payment integrity slice, per ADR 0013 and not
   redesigned here. Proven red first.
5. **Revisit this ADR when the question is answered.** If she asks for a borrower path, it
   needs its own ADR: the delivery channel and the refund call are each larger than this
   decision.

Steps 1 to 3 are days, not weeks. Step 4 is the week-9 slice. Nothing here blocks the freeze
on 2026-08-28.

## Rollback strategy

Steps 1 and 2 are a report field and a documentation sentence; reverting either is a revert
of one commit, and neither is load-bearing for another control.

Step 4 carries the real rollback question, and ADR 0013 already answers it: the key is
nullable and the unique index is partial, so disabling the claim leaves existing rows valid
and the platform returns to today's behaviour — double charges become possible again rather
than the platform becoming unavailable. The failure mode of rollback is the current state,
which is the correct property for a control whose absence is already the baseline.

## Risks and mitigations

**The client reads "prevention shipped" as "double charges are handled."** They are not: the
borrowers already charged twice are unaffected. Mitigated by stating the boundary in the
runbook, in the demo, and in this table rather than only here.

**Prevention slips again and this ADR becomes the record of a decision nobody acted on.**
Mitigated by the gate: the week-9 slice carries a proven-red test, and D19 stays Open in the
debt log with this ADR named, so the entry cannot read as closed.

**The open question is answered with something the platform cannot do.** If she asks for a
borrower notification, the delivery channel is the blocker and it is not ours to close.
Mitigated by asking the question in a form that surfaces the dependency — who owns the
refund is part of the question, because the answer names the team that owns the channel too.

**Attribution leaks personal data into a report that has none.** Mitigated by carrying an
applicant id rather than applicant fields, and by a regression test in the gated suite.

**A duplicate is a legitimate second payment.** Two payments of the same amount seconds apart
are near-certainly one charge twice, but the detector reports a suspect, not a finding, and
this ADR does not change that. Nothing here reverses a payment automatically, which is the
reason not to build the refund path before a human decides.

## Assumptions challenged

We assumed the client's 08-14 answer settled this. It does not: it concerns
`MISSING_IN_LEDGER` and says nothing about a duplicate or about the borrower. Reading it as
settled would have justified building nothing.

We assumed a notification was a small piece of work. It is not: no sender exists in any
service, and the contact columns are unverified. What looked like a feature is a
procurement dependency.

We assumed the interim question needed three answers. It needs one, and asking three invites
three separate deferrals.

## Sign-off status

**Open, and it blocks only the borrower-facing path this ADR defers — notification, refund
submission, and refund status.** When reconciliation finds a duplicate charge, is the
borrower told, is a refund submitted on their behalf, and who owns that refund? Steps 1 to 4
— attribution, the runbook sentence, the ask, and prevention — are unblocked and proceed
without it; only step 5, revisiting this ADR to build that path, waits on the answer.

Two further questions are open in the same area and are tracked with the reconciliation
asks, not here: who receives the alert, and whether the processor returns a reference at
capture.
