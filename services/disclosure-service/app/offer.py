"""Offer assembly.

The origination fee comes from `policies/fee_schedule.json` via rules.py — the same
source apr.py reads, so amount_financed and the APR can no longer be computed from two
different rates inside one offer.
"""

from . import apr, fees


def build_offer(principal: float, annual_rate_pct: float, term_months: int) -> dict:
    a = apr.compute_apr(principal, annual_rate_pct, term_months)
    fc = apr.finance_charge(principal, annual_rate_pct, term_months)
    pmt = apr.monthly_payment(principal, annual_rate_pct, term_months)
    fee = fees.origination_fee(principal)
    return {
        "apr": a,
        "finance_charge": round(fc, 2),
        "monthly_payment": round(pmt, 2),
        "amount_financed": round(principal - fee, 2),
        "total_of_payments": round(pmt * term_months, 2),
    }
