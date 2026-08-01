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

        balance = p
        rows: list[dict] = []
        for n in range(1, term_months + 1):
            interest = (balance * monthly_rate).quantize(CENTS, rounding=ROUND_HALF_UP)
            principal_part = pmt_cents - interest
            balance = balance - principal_part
            if n == term_months:
                # Absorb the residual into the final payment. With Decimal this is the
                # cent-level remainder of quantizing each row, not accumulated float error.
                principal_part += balance
                balance = Decimal(0)
            due = _add_months(start, n)
            rows.append(
                {
                    "n": n,
                    "due_date": due.isoformat(),
                    "payment": float(pmt_cents),
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
