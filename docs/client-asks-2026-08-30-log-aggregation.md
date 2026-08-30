# Client asks — 2026-08-30 working log, log aggregation and retention

**Audience:** Dana (VP Lending Ops, Meridian) · **Sent:** — · **Answered:** —

**Status: DRAFTED, NOT SENT.** No email-fence copy exists yet. The engineering spec these
asks come out of is `docs/spec-log-aggregation.md`; five deliverables in it are implementable
without any answer below, and one — the decision about where logs are collected and stored —
is held until Q2 and Q3 come back.

## Why this is being asked now

When a borrower calls and says a payment did not land, answering them means reconstructing what
the platform did: what the card processor was told, what the servicing system recorded, and in
what order. Today that reconstruction is possible on an engineer's machine and nowhere else.

Three facts, stated plainly because they change what we can promise:

1. **The log files are written to one directory on one machine, and nothing reads them.** There
   is no search, no central copy, and no way for a second person to look at the same evidence.
2. **Nothing removes old log files, and nothing bounds how large they get.** A log directory
   that fills a disk takes the platform down with it. Retention is also a compliance question,
   not only an operational one — see Q1.
3. **The gateway's log is discarded every time its container restarts.** The gateway is the one
   component that sees every request into the platform. Its record is the one not being kept.

None of this is a new defect. It is the part of the 2026-07 logging work (the PII redaction that
shipped in July) that was scoped out at the time and has not been picked up since. The redaction
itself is live and holding: what a log line says today is safe. Where those lines go, how long
they live, and who can search them are the open parts.

**One thing this is not.** We already have tracing on the AI assistant through LangSmith, and it
is genuinely useful for that feature. It does not help here, and expanding it would not change
any of the three facts above: it is configured on one of seven services, it covers the assistant
only, it carries no message content by design, and it is a service outside your network. It
answers questions about the assistant. It answers nothing about a payment.

## The asks

### Q1 — What log retention does Meridian's own policy require?

The spec currently assumes **30 days**, a figure our own debt log proposed and nobody has ever
confirmed against a real policy. If Meridian's records policy, a regulator, or a contract
specifies a different period, that number wins. A longer period changes how large the files are
allowed to get before they roll over; it does not change the design.

**If unanswered:** we size the files to hold roughly 30 days at the log rate we observe, and 30
days becomes a documented assumption rather than a requirement, which is the weaker position at
audit. Worth being precise about what that buys: sizing bounds disk, so a busy day can roll
history off early and a quiet week can keep it late. A period Meridian has to *prove* at audit
needs a scheduled deletion by age, which is a separate piece of work we have not scoped.

### Q2 — May aggregated log data leave Meridian's network?

This is the ask that decides the most, so it is worth being precise about what would be leaving.
Aggregated logs are a much larger and more revealing body of data than the AI traces we send to
LangSmith today, which carry no message content at all. Redacted logs still show which staff
member touched which loan, when, and what the outcome was.

- **"No, it stays inside our network."** The choice narrows to self-hosted collectors and we
  proceed on that basis. This is the answer we expect and the one we would recommend.
- **"Yes, a hosted service is acceptable."** We owe you a vendor comparison and a written
  data-residency position before anything is sent, and that is additional work.

**If unanswered:** we cannot write the decision record at all — see the note under Q3.

### Q3 — Does Meridian already run a log platform we should send to?

If Lending Ops or the wider IT estate already has somewhere logs go — Splunk, an ELK cluster, a
cloud logging service, anything — then the right answer is almost certainly to send there rather
than to stand up something new inside this platform.

The difference is not cosmetic. Sending to an address that already exists is a small, contained
change. Standing up a new store inside this platform means adding a database container, and this
codebase has a security control specifically about database containers not being reachable from
outside — one we have already had to fix four separate times as new ways around it were found.
Every one of those is avoided if you already have a destination.

**If unanswered:** we assume there is nothing to target and design for the larger option, which
is the more expensive guess to be wrong about.

### Q4 — Who may read aggregated logs, and for how long may they hold them?

Once logs are searchable in one place, "who can search them" becomes an access-control decision
rather than an incidental consequence of who has server access. Redaction removes card numbers,
SSNs and contact details, so the stakes are lower than they would otherwise be — but a redacted
log still shows staff activity against named loans, and that is an HR and audit question as much
as a technical one.

**If unanswered:** access defaults to whoever already has infrastructure access, which is
almost certainly broader than you would choose deliberately.

### Q5 — Do log files written before July exist on Meridian-controlled hosts?

Log lines written before the redaction work shipped still contain plaintext card numbers, CVVs
and SSNs. We know those files exist in the development environment and we are building a script
to find and remove them there.

What we do not know is whether the same files exist on any Meridian-controlled host, or in any
backup taken from one. If they do, they need the same treatment, and we cannot see them from
here.

**If unanswered:** we clean what we can see and the rest stays unaddressed, which leaves the
original PCI exposure open somewhere neither of us is looking.

## What proceeds regardless

So that none of the above reads as a request to pause: five of the seven deliverables in the
spec need no answer and are not waiting on this.

| | Deliverable | Held by an ask? |
|---|---|---|
| D1 | Rotation and retention on the log files | Only the number, from Q1 |
| D2 | One logging configuration across all seven services, with a check that keeps it that way | No |
| D3 | The gateway's log survives its container restarting | No |
| D4 | Find and remove the pre-July log files | Scope only, from Q5 |
| D5 | A single request identifier carried across every service, so one payment can be followed end to end | No |
| D6 | Decide where logs are collected and stored | **Yes — Q2 and Q3 both** |
| D7 | Link an AI assistant trace to the matching log lines | No |

D6 is the only one genuinely blocked, and it is blocked on purpose: writing a decision record
that compares three options when two of them are already ruled out by facts we did not ask for
is work we would have to throw away.
