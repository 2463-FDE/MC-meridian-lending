"""APR + finance-charge calculation — Reg Z actuarial method, in Decimal.

What this replaced, and why it mattered:

The previous implementation annualized the finance charge over the FULL initial balance
(`finance_charge / amount_financed / years`) — the add-on method. An installment loan
amortizes, so the borrower does not have the full balance for the full term, and the true
rate is roughly double. On the module's long-standing worked example (18000, 7.99%, 48
months) it disclosed **5.041%** where the actuarial rate is **9.584%**: 4.5pp against a
0.125pp Reg Z tolerance, ~36x. The disclosed APR even printed BELOW the note rate, which
cannot happen on a loan carrying an origination fee.

Float rounding was a real but secondary defect (~0.015pp). Fixing rounding alone would have
left the 4.5pp method error in place, which is why the old docstring's "float 7.142% vs
Decimal 7.157%" framing was misleading — those two numbers reproduce from neither the old
code nor the actuarial method.

The actuarial method (12 CFR 1026 App. J): find the periodic rate that discounts the
payment stream back to the amount financed. Solved here by bisection on the monthly rate —
robust for any positive payment stream, unlike Newton, which needs a good starting guess
and can diverge on short terms.

All computation is Decimal. Callers may pass int/float/str; values are coerced at the
boundary and never re-enter float arithmetic. Money quantizes to cents, APR to 3 decimals,
both ROUND_HALF_UP (the convention a borrower-facing disclosure is read against).
"""

from decimal import Decimal, ROUND_HALF_UP, localcontext

from . import rules

CENTS = Decimal("0.01")
APR_PLACES = Decimal("0.001")
# Working precision for the solve. 28 digits is far beyond the 3 decimals disclosed; the
# margin is what keeps repeated discounting from accumulating error into the answer.
PRECISION = 28
# Monthly-rate search bracket. 0 to 1.0 spans 0% to 1200% nominal annual — no consumer
# installment loan lives outside it, and a wide bracket costs only iterations.
_RATE_LO = Decimal(0)
_RATE_HI = Decimal(1)
# Bisection runs to a fixed iteration count rather than an epsilon: 200 halvings of a
# 1.0-wide bracket resolves far past the 3rd decimal of an annual percentage, and a fixed
# count cannot fail to terminate on a pathological input.
_ITERATIONS = 200


def to_decimal(value) -> Decimal:
    """Coerce at the boundary. str() first — Decimal(0.1) inherits the float's error."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _validate(principal: Decimal, term_months: int) -> None:
    if principal <= 0:
        raise ValueError(f"principal must be positive, got {principal}")
    if term_months <= 0:
        raise ValueError(f"term_months must be positive, got {term_months}")


def monthly_payment(principal, annual_rate_pct, term_months: int) -> Decimal:
    """Level payment for a fixed-rate installment loan, unrounded."""
    principal, rate = to_decimal(principal), to_decimal(annual_rate_pct)
    _validate(principal, term_months)
    with localcontext() as ctx:
        ctx.prec = PRECISION
        r = (rate / 100) / 12
        if r == 0:
            return principal / term_months
        factor = (1 + r) ** term_months
        return principal * r * factor / (factor - 1)


def prepaid_finance_charge(principal) -> Decimal:
    """The origination fee: a prepaid finance charge under Reg Z, withheld at closing."""
    principal = to_decimal(principal)
    with localcontext() as ctx:
        ctx.prec = PRECISION
        fee_pct = to_decimal(rules.get_fee_schedule().origination_fee_pct)
        return (principal * fee_pct).quantize(CENTS, rounding=ROUND_HALF_UP)


def amount_financed(principal) -> Decimal:
    """Principal net of prepaid finance charges — what the borrower actually receives."""
    return to_decimal(principal) - prepaid_finance_charge(principal)


def interest_charge(principal, annual_rate_pct, term_months: int) -> Decimal:
    """Interest only: total of payments less principal. NOT the disclosed finance charge."""
    principal = to_decimal(principal)
    _validate(principal, term_months)
    with localcontext() as ctx:
        ctx.prec = PRECISION
        pmt = monthly_payment(principal, annual_rate_pct, term_months)
        return pmt * term_months - principal


def finance_charge(principal, annual_rate_pct, term_months: int) -> Decimal:
    """Total finance charge per Reg Z 1026.4: interest PLUS prepaid finance charges.

    The previous version returned interest only, understating the disclosed finance charge
    by the whole origination fee ($540 on the worked example). The fee is a cost of credit
    the borrower pays, so it belongs in the disclosed total — and it is already inside the
    APR, so omitting it here also made the two disclosed figures disagree with each other.
    """
    with localcontext() as ctx:
        ctx.prec = PRECISION
        total = interest_charge(principal, annual_rate_pct, term_months)
        total += prepaid_finance_charge(principal)
        return total.quantize(CENTS, rounding=ROUND_HALF_UP)


def _present_value(payment: Decimal, rate: Decimal, term_months: int) -> Decimal:
    """PV of a level annuity-immediate at `rate` per period."""
    if rate == 0:
        return payment * term_months
    return payment * (1 - (1 + rate) ** -term_months) / rate


def compute_apr(principal, annual_rate_pct, term_months: int) -> Decimal:
    """Disclosed APR (percent, 3 decimals) by the Reg Z actuarial method.

    Solves for the monthly rate i where the present value of the payment stream equals the
    amount financed, then annualizes: APR = i * 12 * 100.

    Worked example (18000, 7.99%, 48): payment 439.3481, amount financed 17460.00 (3.0%
    fee), APR **9.584%** — above the 7.99% note rate, as it must be when a fee is financed.
    """
    principal = to_decimal(principal)
    _validate(principal, term_months)
    with localcontext() as ctx:
        ctx.prec = PRECISION
        payment = monthly_payment(principal, annual_rate_pct, term_months)
        financed = amount_financed(principal)
        if financed <= 0:
            raise ValueError(
                f"amount financed must be positive, got {financed} "
                f"(principal {principal} net of prepaid finance charges)"
            )

        # PV decreases monotonically in the rate, so bisection converges without a guess.
        lo, hi = _RATE_LO, _RATE_HI
        for _ in range(_ITERATIONS):
            mid = (lo + hi) / 2
            if _present_value(payment, mid, term_months) > financed:
                lo = mid
            else:
                hi = mid
        monthly = (lo + hi) / 2
        return (monthly * 12 * 100).quantize(APR_PLACES, rounding=ROUND_HALF_UP)
