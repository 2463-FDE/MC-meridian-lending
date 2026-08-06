"""Offer assembly — the Reg Z disclosure box.

Computation happens in Decimal in apr.py; this is the boundary where it becomes float,
because the `offers` columns are DOUBLE PRECISION and the response schema is float (debt
D2). The authoritative minor-unit record lands with the `disclosures` table (spec D3);
until then these floats are the only stored form, so they are quantized to cents here
rather than left to float rounding.

All five figures derive from ONE fee, read from the single versioned schedule — the defect
this replaced had amount_financed on 3.0% and the APR on 2.5% inside the same dict.
"""

from decimal import ROUND_HALF_UP, Decimal

from . import apr

CENTS = Decimal("0.01")


def _cents(value: Decimal) -> float:
    return float(value.quantize(CENTS, rounding=ROUND_HALF_UP))


def build_offer(principal, annual_rate_pct, term_months: int) -> dict:
    pmt = apr.monthly_payment(principal, annual_rate_pct, term_months)
    return {
        # APR is already quantized to 3 decimals by the actuarial solve.
        "apr": float(apr.compute_apr(principal, annual_rate_pct, term_months)),
        "finance_charge": _cents(
            apr.finance_charge(principal, annual_rate_pct, term_months)
        ),
        "monthly_payment": _cents(pmt),
        "amount_financed": _cents(apr.amount_financed(principal)),
        "total_of_payments": _cents(pmt * term_months),
    }
