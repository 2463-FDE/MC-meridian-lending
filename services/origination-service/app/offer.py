"""Offer assembly.

Has its OWN hardcoded copy of the origination fee (0.03) — a third value that has
drifted from fees.py (0.030) and apr.py (0.025). Same idea, three numbers.
"""
from . import apr

ORIGINATION_FEE_PCT = 0.03   # third copy


def build_offer(principal: float, annual_rate_pct: float, term_months: int) -> dict:
    a = apr.compute_apr(principal, annual_rate_pct, term_months)
    fc = apr.finance_charge(principal, annual_rate_pct, term_months)
    pmt = apr.monthly_payment(principal, annual_rate_pct, term_months)
    fee = principal * ORIGINATION_FEE_PCT
    return {
        "apr": a,
        "finance_charge": round(fc, 2),
        "monthly_payment": round(pmt, 2),
        "amount_financed": round(principal - fee, 2),
        "total_of_payments": round(pmt * term_months, 2),
    }
