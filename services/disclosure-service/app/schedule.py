"""Amortization schedule generation (for the disclosure / payment schedule display).

Standard fixed-payment amortization. Uses float math (consistent with the rest of the
platform); fine for a display schedule, but the same float drift that affects apr.py
applies here too.
"""
import datetime
from . import apr


def amortization(principal: float, annual_rate_pct: float, term_months: int,
                 start: datetime.date | None = None) -> list[dict]:
    start = start or datetime.date.today()
    pmt = apr.monthly_payment(principal, annual_rate_pct, term_months)
    monthly_rate = (annual_rate_pct / 100.0) / 12.0
    balance = principal
    rows: list[dict] = []
    for n in range(1, term_months + 1):
        interest = balance * monthly_rate
        principal_part = pmt - interest
        balance = balance - principal_part
        if n == term_months:
            # absorb residual float drift into the final payment
            principal_part += balance
            balance = 0.0
        due = _add_months(start, n)
        rows.append({
            "n": n,
            "due_date": due.isoformat(),
            "payment": round(pmt, 2),
            "principal": round(principal_part, 2),
            "interest": round(interest, 2),
            "balance": round(max(balance, 0.0), 2),
        })
    return rows


def _add_months(d: datetime.date, months: int) -> datetime.date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, 28)
    return datetime.date(year, month, day)
