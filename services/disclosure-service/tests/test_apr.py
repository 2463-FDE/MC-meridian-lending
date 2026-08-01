"""APR money-math tests. (W4 discovery material.)

**This file's tolerance test is now a FALSE GREEN. Do not read it as assurance.**

It compares `compute_apr` against `_decimal_apr`, which re-implements the *same crude
add-on annualization* in Decimal. It therefore only ever measured float-vs-Decimal drift
(~0.015pp) — never whether the method itself is right. It used to fail only because the
two sides used different fee rates (0.025 vs 0.030). Now that both read the one versioned
schedule they agree with each other, and the test passes while the disclosed APR is
5.196% against a Reg Z actuarial 9.584% — 4.4pp, ~35x the tolerance.

That is the shape of the whole Week 4 finding: a green signal over a wrong number. The
real check is the actuarial TILA vectors in a BLOCKING job (spec D1/D7); this file stays
until they land, as the record of what a self-agreeing test proves (nothing).

The D6 half — three drifted ORIGINATION_FEE_PCT copies — is FIXED. apr.py, fees.py and
offer.py now read one versioned rate; see test_rules.py.
"""

from decimal import Decimal, getcontext
from app import apr

getcontext().prec = 28

# Reg Z APR tolerance for a regular transaction is 1/8 of 1 percentage point (0.125).
TILA_APR_TOLERANCE = 0.125


def _decimal_apr(principal, rate_pct, term):
    """Reference APR using Decimal (the 'correct' value the disclosure should show)."""
    p = Decimal(principal)
    r = Decimal(rate_pct) / 100 / 12
    n = term
    factor = (1 + r) ** n
    pmt = p * r * factor / (factor - 1)
    fee = p * Decimal("0.030")  # policy fee = 3.0%
    fc = pmt * n - p + fee
    amount_financed = p - fee
    apr_val = (fc / amount_financed) / (Decimal(term) / 12) * 100
    return float(apr_val)


def test_apr_within_tila_tolerance():
    principal, rate, term = 18000, 7.99, 48
    disclosed = apr.compute_apr(principal, rate, term)
    reference = _decimal_apr(principal, rate, term)
    assert abs(disclosed - reference) <= TILA_APR_TOLERANCE, (
        f"disclosed APR {disclosed} vs reference {reference} exceeds Reg Z tolerance"
    )


# The three-drifted-constants half of this file (D6) is FIXED: apr.py, fees.py and offer.py
# now read one versioned rate from policies/fee_schedule.json. The replacement assertions —
# constants gone, one rate reaching both APR and amount_financed, loader fails closed — live
# in test_rules.py. The APR-tolerance test above still fails by design until the actuarial
# method lands (spec D1).
