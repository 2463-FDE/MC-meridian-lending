# Cards — settlement reconciliation (deferred work)

Work the week-7 reconciliation control names but does not build. Same format as
`docs/cards-week6-servicing.md` and `docs/cards-week8-governance.md`: what it is, what
exists today, and why it is not in this cycle.

## R1 — One ledger snapshot per run

**What.** `reconcile()` reads the ledger once, over the widest window any consumer needs,
and derives the matching pool, the true-window totals and the duplicate scan from that one
in-memory set — or runs every read inside a repeatable-read transaction.

**What exists today.** Two separate `load_ledger()` calls with no transaction or snapshot
boundary between them: one for matching and variance over the tolerance-widened window
(`services/servicing-service/app/reconciliation.py`), a second for duplicate detection over
the duplicate-window-widened one. On a live `payments` table a row inserted between the two
`SELECT`s can raise the duplicate count without appearing in `matched_count`, `breaks` or
either variance figure, and a row inserted after the second read is missed by both. The
result is then internally inconsistent — each field describes a slightly different database
state — which is confusing to investigate rather than wrong in a way that loses money.

**Why not this cycle.** Raised as `[medium]` in review while the matcher PR was being frozen
for merge. The fix rewrites how every ledger pool in the run is derived, on the money-control
path, and no test can demonstrate the race — only that the run issues one query instead of
two — so it would ship a structural change to a control with the weakest possible evidence
behind it. The exposure is bounded: this job is schedulable rather than scheduled (D3(a)),
month-end runs read a window that closed days earlier, and reconciliation is read-only, so a
concurrent insert during a run is an operator running it against live intake rather than
normal use. Worth doing, worth doing with the ledger-read path's own tests around it.

**Size.** Half a day, including a query-count regression test and a re-run of the matching
and duplicate vectors against the single-snapshot derivation.

## R2 — Where 120 seconds came from

**What.** `DUPLICATE_SUSPECT_WINDOW_SECONDS` carries a recorded reason for its value, or the
value changes to one that has one.

**What exists today.** `.env.example` sets it to 120 with a two-line comment that says what
the bound does and not why it is 120. The bound decides which pairs of same-loan, same-amount
`payments` rows are reported as a suspected double charge, so its value is a control
threshold: too tight and a slow retry is never flagged, too loose and a legitimate second
payment is. Nothing in the tree explains the number a reader would have to defend.

The reasoning does exist, on the superseded branch archived as tag
`archive/duplicate-detection-week7`. That commit set the value to 300 and argued it: an
amortized schedule generates one due date per calendar month, so two legitimate equal-amount
rows on one loan are at least a billing cycle apart, and 300s covers the retry/replay case
(the seeded duplicate is 2 seconds apart) without approaching a monthly recurrence. That
branch's feature was re-implemented on the matcher branch and merged as part of #39; the
value and its justification were not carried across.

**Why not this cycle.** Changing a control threshold is a decision, not a cleanup, and the
client has an open question on reconciliation constants already (spec D4's alert threshold).
Both belong in the same answer rather than one being changed unilaterally while the other
waits.

**Size.** An hour to restore the rationale at 120 or move to 300, plus whichever way it goes,
a line in the runbook stating the reasoning so an operator tuning it knows what it trades.

## R3 — Read the duplicate window at call time

**What.** `reconciliation` resolves `DUPLICATE_SUSPECT_WINDOW_SECONDS` when the job runs, not
when the module is imported.

**What exists today.** `config.py` binds it at import
(`DUPLICATE_SUSPECT_WINDOW_SECONDS = os.getenv(...)`), so a deployed value change needs a
process restart to take effect, and any caller that sets the variable after import silently
gets the old one. This is not hypothetical: the D3 report's own test fixture set it through
the environment and every CLI test aborted with exit 2 over an "unset" bound that was in fact
set — fixed by patching the module attribute instead, which is a test working around the
binding rather than the binding being right.

The superseded branch archived as `archive/duplicate-detection-week7` read it at call time
for exactly this reason. `main`'s version is otherwise stronger — it adds the 30-day ceiling
and the `/health` rung — so this is one axis, not a wholesale revert.

**Why not this cycle.** It touches config resolution for a money control, and the failure it
prevents is operational (a stale value after a config change) rather than a wrong figure. It
also wants doing alongside the same seam's other callers rather than for one variable.

**Size.** Two hours, including a test that changes the deployed value between two runs in one
process and asserts the second run sees it.
