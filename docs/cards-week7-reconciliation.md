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
