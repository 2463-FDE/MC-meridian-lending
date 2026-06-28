"""Amortization-schedule tests (these PASS)."""
from app import schedule


def test_schedule_length_matches_term():
    rows = schedule.amortization(15000, 7.142, 36)
    assert len(rows) == 36


def test_schedule_amortizes_to_zero():
    rows = schedule.amortization(20000, 10.0, 60)
    assert rows[-1]["balance"] == 0.0


def test_monthly_payment_positive():
    assert schedule.monthly_payment(10000, 8.5, 48) > 0
