# Meridian Lending — Fee Schedule (internal)

*Last reviewed: 2024-11. Owner: Lending Ops.*

> These published values are the human-facing source of truth. The code no longer hardcodes
> its own copies: the drifted constants that used to live in `apr.py` (0.025), `fees.py`, and
> `offer.py` (0.03) were replaced by a single versioned loader — `apr.py`, `fees.py`, and
> `offer.py` all read `policies/fee_schedule.json` via `rules.get_fee_schedule()`, which fails
> closed. `tests/test_rules.py` asserts this markdown and the JSON agree, so the two cannot
> drift the way the three hardcoded copies did. Keep this table and `fee_schedule.json` in step.

| Fee | Amount |
|-----|--------|
| Origination fee | 3.0% of principal |
| Late payment fee | $35 flat, or 5% of the past-due amount, whichever is **less** |
| Returned payment (NSF) | $25 |
| Payoff statement | $0 |

## APR / finance charge

- APR is the annualized cost of credit including the finance charge per Reg Z.
- Disclosed APR and finance charge are subject to **Reg Z tolerances**; a disclosed APR
  that differs from the actual by more than the regulatory tolerance is a violation.
- The payment waterfall on a received payment is: **fees → accrued interest → principal.**

## Interest

- Interest accrues on the outstanding principal at the loan's note rate.
- Standard personal-installment note rates: 7.99% – 24.99% APR by risk band.
