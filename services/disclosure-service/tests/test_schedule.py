"""Amortization-schedule tests (these PASS — the schedule generator is sound).

Note the coverage GAP: there is no test that the *disclosed APR/finance charge* match a
Decimal/TILA reference (that lives in test_apr.py and currently fails), and nothing tests
the offer endpoint end-to-end against a DB.
"""
from app import schedule


def test_schedule_has_one_row_per_month():
    rows = schedule.amortization(12000, 9.99, 36)
    assert len(rows) == 36
    assert rows[0]["n"] == 1
    assert rows[-1]["n"] == 36


def test_schedule_ends_at_zero_balance():
    rows = schedule.amortization(18000, 7.99, 48)
    assert rows[-1]["balance"] == 0.0


def test_schedule_payments_are_positive():
    rows = schedule.amortization(5000, 12.5, 24)
    assert all(r["payment"] > 0 for r in rows)
    assert all(r["interest"] >= 0 for r in rows)
