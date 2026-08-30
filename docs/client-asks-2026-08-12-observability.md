# Client asks — observability + reconciliation phase

**Audience:** Dana (VP Lending Ops, Meridian) · **Written:** 2026-08-12

| # | Ask | Dana's response |
|---|-----|---|
| 1 | **Owner needed now:** $500 on loan 4471 — two settled captures (PR-100290, PR-100311) with no matching ledger row. Customers were charged and their balances don't reflect it. Already known/written off, or open? | — |
| 2 | Processor reference number: does the card processor return one at capture, and in which field? Platform never records it today, so matching is guessed from loan/amount/date. Exact key at capture time = exact reconciliation; reference only appearing next-day in settlement = approximate matching, manual review needed. Different builds — which one? | — |
| 3 | Is the settlement file sent (1–7 June, 12 rows) the full month, or a sample? Figures above (net −888.82 on 3,174.17 settled) will change if the real file is 30 days. | — |
| 4 | You described the month-end gap as small/persistent; in this extract it's 28% of settled amount. Is the sample unrepresentative, or is "small" judged against a much larger portfolio? Matters for the alert threshold. | — |
| 5 | What size of difference does finance consider acceptable to close on? Need a number to alert against — alerting on any difference at all would fire daily from day one. | — |
| 6 | Who receives the alert, and what time of day? Also: cut-off convention for month-end — processor-settled day or our-recorded day, which time zone? Some of the gap above may be cutoff timing rather than lost money. | — |
