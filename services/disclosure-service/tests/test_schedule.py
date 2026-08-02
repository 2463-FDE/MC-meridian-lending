"""Amortization-schedule tests (these PASS — the schedule generator is sound).

Note the coverage GAP: there is no test that the *disclosed APR/finance charge* match a
Decimal/TILA reference (that lives in test_apr.py and currently fails), and nothing tests
the offer endpoint end-to-end against a DB.
"""
import pytest
from decimal import Decimal

from app import apr, schedule


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


class TestScheduleReconcilesWithTheDisclosedTotal:
    """Reg Z 1026.18(g)/(h): the payment schedule and the Total of Payments are two
    disclosures of the same loan, and they have to agree.

    They did not. Total of Payments is derived from the UNROUNDED payment (amount financed
    plus finance charge), so it is not `payment x term`: 48 x 439.35 = 21,088.80 against a
    disclosed 21,088.71. The old schedule showed 48 level payments summing to the former,
    and folded the 9-cent residual into the final row's PRINCIPAL while leaving that row's
    `payment` at the level amount — so the last row disclosed a payment that did not equal
    its own principal-plus-interest split, by up to 34 cents on the cases tested here.

    The difference now goes where the industry puts it: a final payment that differs from
    the level payment, disclosed as such.
    """

    CASES = [
        (18000, 7.99, 48),
        (15000, 7.99, 36),
        (5000, 12.5, 12),
        (50000, 3.0, 60),
        (1200, 35.0, 12),
        (2000, 7.99, 12),
    ]

    @pytest.mark.parametrize("principal,rate,term", CASES)
    def test_the_rows_sum_to_the_disclosed_total_of_payments(self, principal, rate, term):
        rows = schedule.amortization(principal, rate, term)
        disclosed = apr.amount_financed(principal) + apr.finance_charge(
            principal, rate, term
        )
        assert sum(Decimal(str(r["payment"])) for r in rows) == disclosed

    @pytest.mark.parametrize("principal,rate,term", CASES)
    def test_every_row_pays_exactly_its_principal_plus_interest(
        self, principal, rate, term
    ):
        """Including the last one — that is the row that used to disagree with itself."""
        for row in schedule.amortization(principal, rate, term):
            assert Decimal(str(row["payment"])) == Decimal(str(row["principal"])) + Decimal(
                str(row["interest"])
            ), row

    @pytest.mark.parametrize("principal,rate,term", CASES)
    def test_the_loan_closes_at_zero(self, principal, rate, term):
        assert schedule.amortization(principal, rate, term)[-1]["balance"] == 0.0

    def test_only_the_final_payment_differs_from_the_level_payment(self):
        """The borrower is told one recurring amount; exactly one payment may differ."""
        rows = schedule.amortization(18000, 7.99, 48)
        level = {r["payment"] for r in rows[:-1]}
        assert level == {439.35}
        assert rows[-1]["payment"] == 439.26
