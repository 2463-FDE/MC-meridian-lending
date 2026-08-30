# Client asks — payments phase (week 5)

**Audience:** Dana (VP Lending Ops, Meridian) · **Written:** 2026-08-08

| # | Ask | Dana's response |
|---|-----|---|
| 1 | Stored card data: platform stores full PAN + CVV in plaintext since go-live. PCI DSS bars CVV retention post-authorization. Who owns the purge, and when? | — |
| 2 | Double-charge tickets: three customers hit by the reproduced concurrency defect (two concurrent payments both charge, balance reflects only one). Were they made whole? | — |
| 3 | APR method of record: we compute actuarial (Reg Z Appendix J); could not reproduce the brief's worked figure that way, only via add-on annualization (~4.5pp difference). Confirm actuarial is correct, or point us at the source the brief used. | — |
| 4 | Tolerance regime: enforcing regular-transaction tolerance (0.125pp). Confirm, or specify if irregular-transaction tolerance (0.25pp) applies. | — |
| 5 | Back book: fix is forward-only, no auto-recompute of delivered disclosures. Did the earlier (add-on) APR figure reach borrowers, and do those disclosures need curing? | — |
| 6 | PCI scope: hosted-fields processor keeps scope near SAQ-A (3–5 days); card data landing on our servers pushes scope to SAQ-D (multi-week). Which should we plan for? | — |
| 7 | Processor: which card processor, and does it offer hosted fields / client-side token exchange? | — |
| 8 | Borrower contact channel: pay-by-link needs a verified channel to reach the borrower. Does Meridian send statements/due-date notices today, and through what channel? | — |
| 9 | Card on file: is brand + last four + expiry sufficient for CSR caller verification? | — |
| 10 | ACH wording: is "payment submitted" (with settlement confirmed separately) the right borrower-facing status? | — |
| 11 | Duplicate window: is a 24h configurable retry window correct, or does a same-day network retry need to be distinguished from a next-day repeat click? | — |
| 12 | Pay-by-link lifetime: is one statement cycle (configurable) correct? | — |
