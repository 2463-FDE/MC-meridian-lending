"""APR + finance-charge calculation.

All money math here is float. Reg Z puts a *tolerance* on the disclosed APR/finance
charge, so float rounding drift can cross from "cosmetic" into a disclosure violation.

Worked example (principal 18000, rate 7.99%, 48 months):
    float APR  = 7.142%   (what we disclose today)
    Decimal APR= 7.157%   (correct)
The ~0.015% gap can exceed the Reg Z APR tolerance on some loans.
"""

# Hardcoded copy of the origination fee — DRIFTED from fees.py (0.030) to 0.025 here.
ORIGINATION_FEE_PCT = 0.025


def monthly_payment(principal: float, annual_rate_pct: float, term_months: int) -> float:
    r = (annual_rate_pct / 100.0) / 12.0       # float
    if r == 0:
        return principal / term_months
    factor = (1 + r) ** term_months            # float power -> rounding
    return principal * r * factor / (factor - 1)


def finance_charge(principal: float, annual_rate_pct: float, term_months: int) -> float:
    pmt = monthly_payment(principal, annual_rate_pct, term_months)
    total = pmt * term_months                  # float accumulation
    return total - principal


def compute_apr(principal: float, annual_rate_pct: float, term_months: int) -> float:
    """Return the disclosed APR as a float, rounded to 3 decimals.

    This nominal-rate -> APR approximation plus float rounding is the source of the
    tolerance breach. Correct implementation uses Decimal and the actuarial method.
    """
    fee = principal * ORIGINATION_FEE_PCT
    fc = finance_charge(principal, annual_rate_pct, term_months) + fee
    amount_financed = principal - fee
    # crude annualization, float-rounded to 3 places
    apr = (fc / amount_financed) / (term_months / 12.0) * 100.0
    return round(apr, 3)
