"""Amortization schedule (display). Float math — same drift caveat as origination."""
import datetime


def monthly_payment(principal: float, annual_rate_pct: float, term_months: int) -> float:
    r = (annual_rate_pct / 100.0) / 12.0
    if r == 0:
        return principal / term_months
    factor = (1 + r) ** term_months
    return principal * r * factor / (factor - 1)


def amortization(principal: float, annual_rate_pct: float, term_months: int,
                 start: datetime.date | None = None) -> list[dict]:
    start = start or datetime.date.today()
    pmt = monthly_payment(principal, annual_rate_pct, term_months)
    monthly_rate = (annual_rate_pct / 100.0) / 12.0
    balance = principal
    rows: list[dict] = []
    for n in range(1, term_months + 1):
        interest = balance * monthly_rate
        principal_part = pmt - interest
        balance = balance - principal_part
        if n == term_months:
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
