"""Amortization schedule generation (for the disclosure / payment schedule display).

Standard fixed-payment amortization, computed in Decimal alongside apr.py — the schedule a
borrower reads has to agree with the disclosed payment, and float drift across dozens of
rows is how it stops agreeing. Rows quantize to cents and are handed out as floats, because
the response schema and the float `offers` columns are the boundary (debt D2).
"""

import datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext

from . import apr

CENTS = Decimal("0.01")


def amortization(
    principal, annual_rate_pct, term_months: int, start: datetime.date | None = None
) -> list[dict]:
    start = start or datetime.date.today()
    with localcontext() as ctx:
        ctx.prec = apr.PRECISION
        p = apr.to_decimal(principal)
        pmt = apr.monthly_payment(p, annual_rate_pct, term_months)
        pmt_cents = pmt.quantize(CENTS, rounding=ROUND_HALF_UP)
        monthly_rate = (apr.to_decimal(annual_rate_pct) / 100) / 12

        # The disclosed Total of Payments (Reg Z 1026.18(h) — amount financed plus finance
        # charge) is derived from the UNROUNDED payment, so it is not `pmt_cents * term`.
        # The schedule has to sum to it, or the borrower reads two different totals in one
        # document: 48 x 439.35 = 21,088.80 against a disclosed 21,088.71. The difference
        # goes where the industry puts it — into a final payment that differs from the
        # level payment, disclosed as such.
        total_of_payments = (pmt * term_months).quantize(CENTS, rounding=ROUND_HALF_UP)
        final_payment = total_of_payments - pmt_cents * (term_months - 1)

        balance = p
        rows: list[dict] = []
        for n in range(1, term_months + 1):
            interest = (balance * monthly_rate).quantize(CENTS, rounding=ROUND_HALF_UP)
            payment = pmt_cents
            principal_part = pmt_cents - interest
            balance = balance - principal_part
            if n == term_months:
                # The final row closes the loan AND squares the total. Principal is
                # whatever is still outstanding, so the balance reaches exactly zero;
                # interest is the remainder of the final payment, so payment still equals
                # principal + interest. Previously the residual was folded into principal
                # while `payment` kept the level amount, leaving the last row disclosing a
                # payment that did not equal its own principal-plus-interest split.
                payment = final_payment
                principal_part += balance
                balance = Decimal(0)
                interest = payment - principal_part
            due = _add_months(start, n)
            rows.append(
                {
                    "n": n,
                    "due_date": due.isoformat(),
                    "payment": float(payment),
                    "principal": float(
                        principal_part.quantize(CENTS, rounding=ROUND_HALF_UP)
                    ),
                    "interest": float(interest),
                    "balance": float(
                        max(balance, Decimal(0)).quantize(CENTS, rounding=ROUND_HALF_UP)
                    ),
                }
            )
        return rows


def _add_months(d: datetime.date, months: int) -> datetime.date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, 28)
    return datetime.date(year, month, day)
