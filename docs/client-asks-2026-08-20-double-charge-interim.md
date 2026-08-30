# Client asks — 2026-08-20, the alert recipient and the double-charge interim

**Audience:** Dana (VP Lending Ops, Meridian) · **Written:** 2026-08-20 ·
**Cycle:** code freeze **Wednesday 2026-09-02** (moved from Friday 2026-08-28 by the 2026-08-21 program email), handoff Friday 2026-09-04

**Send status — corrected 2026-08-21. Both asks are still unsent, and "email 2" no longer exists
as a thing to send.** Asks 14 and 15 remain rows in the ask table of
`docs/client-asks-2026-08-13-consolidated.md`, which is still the single place to read what is
outstanding; this document holds their reasoning and the original handoff copy.

**The recommendation below this line is void.** It read *"send email 2 now, carrying all five
asks."* Three of those five — the export source, the print-and-mail vendor and the policy corpus —
had **already gone out on 2026-08-16**, in an email whose artifacts existed only in unregistered
worktrees until they were recovered on 08-21. Dana replied on 08-17 and **deferred all three to
2026-08-28**. So the five-ask envelope this document recommended was never available: two of its
five were unasked, and the other three were asked and deferred. Acting on it would have re-asked
her deferrals.

**Where asks 14 and 15 now sit.** Both are carried in `docs/client-asks-2026-08-21-final.md`, and
both are **deliberately excluded from the send going out before freeze**. That send is three items,
all of which change what gets built this week: two questions the 08-17 reply itself raised, plus a
one-line confirmation. Neither 14 nor 15 blocks the freeze scope, and putting five items in front of
her buys a partial answer where three buys a full one. **They go with the 08-28 batch**, alongside
the questions she deferred there herself.

**What still stands in this document, unchanged:** the reasoning for both asks, the verified
statement of today's position on duplicate charges, and the handoff copy at the end — ask 14's
opening paragraph in particular, which owns the miss rather than letting her think she already
answered it.

**Source:** week-8 client-demo feedback, improvement priorities 1 and 2, plus an audit of the
08-13 consolidated ask against what was actually sent. **Consumes these answers:**
`adr/0018-interim-handling-of-a-double-charged-borrower.md` (ask 15 is its sign-off),
`docs/runbook.md` (ask 14).

---

## Why only two, when the feedback names three

The feedback lists three open client questions and scores them 0 of 3. Checked against the
record, that count is wrong in both directions:

- **The threshold is answered.** She gave 500 minor units per daily close on 2026-08-14. It is
  built and gated. The feedback's Q1 bundles it with the recipient; only the recipient half is
  open.
- **The interim offer is half answered**, and answered restrictively — see ask 15.
- **The processor reference (ask 10) is parked to next cycle on purpose**, not overlooked. Spec
  D2(c) matches on `(loan_id, amount_minor, ±1 day)` and works; an exact key would replace
  approximate matching, which is an improvement, not a blocker. Raising it now reverses a
  considered deferral and spends a question we will want next cycle. **Not asked here.**

So two asks, not three. The one genuinely new fact is how ask 14 went missing.

---

## Ask 14 — Who receives the reconciliation alert, when, and through what channel?

**Status: never asked.** Not deferred, not stated, not answered — lost in a split.

The 08-12 observability draft's row 6 asked two things in one row: *"Who receives the alert,
and what time of day? Also: cut-off convention for month-end — processor-settled day or
our-recorded day, which time zone?"* When the 08-13 consolidated ask was assembled, the
cut-off half became ask 2 and was answered on 08-14. **The recipient half is in no ask table,
no stated-assumption list, and no deferral row.** It is not in the 0-of-3 count for a reason
that has nothing to do with priority.

**What it blocks.** Nothing in code. Everything in effect. `docs/runbook.md:168` mails
`ops@example.com`, a placeholder. The control detects correctly, exits non-zero, and reports
to nobody. On this branch the runbook now says so where the placeholder appears, because a
runbook that shows an address implies someone reads it.

**What we need:** a distribution list or a named owner, a time of day, and a channel. Email
to a shared mailbox is the assumption if she has no preference — it is what the cron example
already does and it needs no new integration.

**Consequence of no answer:** the cron ships commented out rather than wired to a placeholder,
and the alert stays a thing an operator runs by hand.

---

## Ask 15 — When a duplicate charge is found, is the borrower told, is a refund submitted, and who owns it?

**Status: half answered, and the answered half constrains the rest.** This is ADR 0018's
sign-off question. Asked as one question on purpose: the feedback frames it as three
(prevention, notification, refund status), and three invites three separate deferrals.

**What she already decided, on 2026-08-14.** Asked who owned the $500 on loan 4471: open
exception rather than a write-off, both processor references in the exception report, marked
for Finance Ops review, and — her words — *"no separate remediation or ticketing workflow is
needed in this build."* That settles the operator side and rules out a ticketing workflow.
ADR 0018 respects it rather than reopening it, and the runbook now names Finance Ops as owner.

**Why that does not close it.** Two reasons, and both are why this is a separate ask rather
than an assumption we could state:

1. Her answer concerns `MISSING_IN_LEDGER` — money captured and never credited. A duplicate
   capture is a different break class, reported separately and excluded from the variance
   figures because the money is already counted on whichever side it landed.
2. It says nothing about the borrower. Every part of her answer is about what an operator sees.

**Today's position, stated so the ask is not read as a menu.** Verified against `main`, not
assumed:

| | Today |
|---|---|
| Repeated capture prevented | **No.** Every `POST /payments` inserts a row; there is no idempotency key |
| Borrower notified | **No, and no channel exists** — no outbound sender in any service, and the contact columns are unverified |
| Refund submitted | **No.** No refund, void, or reversal path, and the processor client exposes none |
| Refund status visible | **Nowhere** |
| Duplicate detected | **Yes**, as a pair, including across a window edge |

**What we are doing without waiting for her.** Prevention at capture — ADR 0013's idempotency
key — is the interim protection and ships first, in the week-9 payment integrity slice. It
needs no answer from her: nobody has to approve not double-charging people. It does nothing
for the borrowers already charged twice, which is exactly why ask 15 matters.

**What her answer decides.** Whether the borrower-facing half gets built at all, and if so,
what it is. Two things to say plainly when asking, because they change the shape of the
answer rather than its cost:

- **A notification needs a delivery channel this platform does not have.** No outbound sender
  exists anywhere, and `applicants.email` / `applicants.phone` are unverified columns. This is
  the same gap as the week-5 pay-by-link question. If she wants the borrower told, that is a
  procurement dependency first and a feature second.
- **A refund needs a processor call that does not exist.** The payment service cannot ask the
  processor to reverse anything today.

So the honest form of the ask is: **does the borrower get told, does a refund get submitted on
their behalf, and who owns that refund** — with the understanding that a yes to either of the
first two starts with naming a channel or a processor capability, not with us writing code.

**Consequence of no answer:** ADR 0018 stops at step 3 (attribution, the runbook sentence,
this ask). Prevention still ships. A borrower charged twice today is told by nobody and finds
out on a statement, which is the current state — named rather than discovered.

---

## Handoff copy — append to email 2

_The email body for this section is held outside this repository with the other client-facing copies. This log keeps the reasoning, the decisions and her replies; the sent text is not published here._

## What this does not ask

- **The processor reference at capture (ask 10).** Parked to next cycle by the 08-13 ask, on
  purpose. Matching works without it.
- **Anything already in email 2.** The export source, the print-and-mail vendor, and the policy
  corpus stand as written.
- **Backup and recovery.** Now recorded in `docs/runbook.md` as absent, with the client-side
  half named — who takes backups of production, on what schedule, stored where, and whether
  any predate the card-data work. It belongs in next cycle's ask, not this email: it is a
  larger conversation than two lines, and nothing before freeze turns on it.
