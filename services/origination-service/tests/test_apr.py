"""APR money-math tests.

These currently FAIL because apr.py uses float and a crude annualization. CI runs them
with continue-on-error so the build stays green. (This is the W4 discovery material.)
"""
from decimal import Decimal, getcontext
from app import apr

getcontext().prec = 28

# Reg Z APR tolerance for a regular transaction is 1/8 of 1 percentage point (0.125).
TILA_APR_TOLERANCE = 0.125


def _decimal_apr(principal, rate_pct, term):
    """Reference APR using Decimal (the 'correct' value the disclosure should show)."""
    p = Decimal(principal)
    r = (Decimal(rate_pct) / 100 / 12)
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


def test_fee_constants_agree_across_modules():
    from app import fees, offer
    assert fees.ORIGINATION_FEE_PCT == apr.ORIGINATION_FEE_PCT == offer.ORIGINATION_FEE_PCT
