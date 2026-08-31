# Client asks — week 6 (servicing money controls)

**Audience:** Dana (VP Lending Ops, Meridian) · **Written:** 2026-08-08

| # | Ask | Dana's response |
|---|-----|---|
| 1 | Unauthorized balance access: any signed-in user, including borrowers, can currently adjust any loan's balance or waive a fee — the endpoints accept a role and ignore it. Close this now as a small separate fix, ahead of the dashboard? Reachable from your live environment, or only internally? | — |
| 2 | Balance history: does not exist and cannot be backfilled — balance is a single overwritten field, audit table is empty. Is there a pending controller/audit/examiner request that assumes this history exists? | — |
| 3 | Who may move money: assumption is CSRs and admins may adjust/waive, underwriters and borrowers may not. Is CSR the right level, or should this sit with a supervisor role? | — |
| 4 | Second approver: assumption is a second person approves only manual discretionary moves (adjustment, waiver) — not automated ones. No dollar threshold unless you want one. Confirm, or give a threshold. | — |
| 5 | Approver identity / break-glass: assumption is any other CSR/admin may approve, nobody self-approves, no override. What happens if a rep is alone with no second approver available? | — |
| 6 | Reason codes: every adjustment/waiver will require a reason. Existing list from the legacy system/ops manual/QA checklist, or free text? | — |
| 7 | Opening balance: assumption is today's stored balance becomes the opening ledger figure (known-imperfect, same concurrency defect as the double-charge tickets), vs. a reconciliation pass before cutover. Which do you want? | — |
| 8 | Volume and approver coverage: roughly how many balance adjustments/waivers per day or week, how many reps do them, how many people are available to approve? | — |
| 9 | Manual late fees: do reps ever apply/reverse a late fee by hand, or is it fully automated by rule? | — |
| 10 | Existing waiver authority: is there a written limit today on what a rep may waive/adjust without escalation (dollar cap, per-account cap, monthly cap)? | — |
| 11 | Borrower notification: does a borrower get notified when a rep adjusts their balance or waives a fee — statement line, email, or nothing? | — |
| 12 | History retention: how far back does balance history need to be queryable, and is there a retention period you're held to? | — |
| 13 | SOX contact: README lists Sam as the SOX/reconciliation contact — can Sam confirm the corrected audit-trail statements are accurate from his side? | — |
