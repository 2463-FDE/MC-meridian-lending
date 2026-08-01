"""APR + finance-charge tests — Reg Z actuarial method.

This file used to hold the Week 4 discovery material: a tolerance test that compared
`compute_apr` against a Decimal re-implementation of the SAME crude add-on formula. It
measured float drift (~0.015pp) and could never detect the 4.5pp method error — a test
that agreed with the bug. It is replaced here with assertions against an independently
derived actuarial reference.

The multi-loan vector table and its blocking CI job land with spec D7; these are the
unit-level invariants the implementation must hold regardless.
"""

from decimal import Decimal

import pytest

from app import apr, rules

# Reg Z regular-transaction tolerance, from the same policy file the code reads, so a
# change to the tolerance regime does not need a code edit (Dana: question 2).
TOLERANCE = Decimal(str(rules.load_fee_schedule().apr_tolerance_pp))


def _reference_apr(principal, annual_rate_pct, term_months, fee_pct):
    """Independent actuarial reference: bisect the rate that PVs payments to the amount
    financed. Written from the definition rather than by calling apr.py, so a bug in the
    implementation cannot hide behind a reference that shares it."""
    p = Decimal(str(principal))
    r = Decimal(str(annual_rate_pct)) / 100 / 12
    n = term_months
    pmt = p / n if r == 0 else p * r * (1 + r) ** n / ((1 + r) ** n - 1)
    financed = p - (p * Decimal(str(fee_pct))).quantize(Decimal("0.01"))
    lo, hi = Decimal(0), Decimal(1)
    for _ in range(200):
        mid = (lo + hi) / 2
        pv = pmt * n if mid == 0 else pmt * (1 - (1 + mid) ** -n) / mid
        if pv > financed:
            lo = mid
        else:
            hi = mid
    return ((lo + hi) / 2 * 12 * 100).quantize(Decimal("0.001"))


def test_worked_example_matches_actuarial_reference():
    """18000 / 7.99% / 48mo — the loan the old docstring got wrong.

    Shipped 5.041% under the add-on method; the actuarial rate is 9.584%.
    """
    disclosed = apr.compute_apr(18000, 7.99, 48)
    assert disclosed == Decimal("9.584")
    assert abs(disclosed - _reference_apr(18000, 7.99, 48, 0.030)) <= TOLERANCE


@pytest.mark.parametrize(
    "principal,rate,term",
    [
        (5000, 12.5, 24),
        (12000, 9.99, 36),
        (18000, 7.99, 48),
        (25000, 24.99, 60),
        (3000, 7.99, 12),
        (40000, 15.0, 84),
    ],
)
def test_within_tolerance_of_reference_across_the_book(principal, rate, term):
    disclosed = apr.compute_apr(principal, rate, term)
    reference = _reference_apr(principal, rate, term, 0.030)
    assert abs(disclosed - reference) <= TOLERANCE, (
        f"disclosed {disclosed} vs reference {reference} exceeds {TOLERANCE}pp"
    )


@pytest.mark.parametrize(
    "principal,rate,term",
    [(5000, 12.5, 24), (18000, 7.99, 48), (25000, 24.99, 60), (40000, 15.0, 84)],
)
def test_apr_never_prints_below_the_note_rate(principal, rate, term):
    """The tell that exposed the old method: 5.041% disclosed on a 7.99% note.

    A financed origination fee can only push the cost of credit above the note rate.
    """
    assert apr.compute_apr(principal, rate, term) > Decimal(str(rate))


def test_zero_fee_collapses_apr_to_the_note_rate(monkeypatch):
    """With no prepaid finance charge, APR is the note rate — isolates the fee's effect."""
    schedule = rules.load_fee_schedule()
    monkeypatch.setattr(
        rules,
        "get_fee_schedule",
        lambda: type(schedule)(**{**vars(schedule), "origination_fee_pct": 0.0}),
    )
    assert apr.compute_apr(18000, 7.99, 48) == Decimal("7.990")


def test_fee_on_a_zero_interest_loan_still_carries_an_apr():
    """0% note rate is not 0% APR when a fee is withheld — the fee IS the cost of credit."""
    assert apr.compute_apr(12000, 0, 24) > 0


def test_finance_charge_includes_the_prepaid_fee():
    """Reg Z 1026.4. The old version disclosed interest only, understating by the fee."""
    interest = apr.interest_charge(18000, 7.99, 48)
    fee = apr.prepaid_finance_charge(18000)
    assert apr.finance_charge(18000, 7.99, 48) == (interest + fee).quantize(
        Decimal("0.01")
    )
    assert apr.finance_charge(18000, 7.99, 48) == Decimal("3628.71")


def test_amount_financed_is_principal_net_of_the_fee():
    assert apr.amount_financed(18000) == Decimal("17460.00")


def test_computation_returns_decimal_not_float():
    """The compute path must not hand floats onward; float appears only at the boundary."""
    for value in (
        apr.compute_apr(18000, 7.99, 48),
        apr.finance_charge(18000, 7.99, 48),
        apr.monthly_payment(18000, 7.99, 48),
        apr.amount_financed(18000),
        apr.prepaid_finance_charge(18000),
    ):
        assert isinstance(value, Decimal)


def test_float_input_does_not_poison_the_decimal_path():
    """Decimal(0.1) inherits the float's error; Decimal(str(0.1)) does not."""
    assert apr.compute_apr(18000.0, 7.99, 48) == apr.compute_apr("18000", "7.99", 48)


@pytest.mark.parametrize(
    "principal,term", [(0, 48), (-1, 48), (18000, 0), (18000, -12)]
)
def test_degenerate_inputs_raise_rather_than_disclose_nonsense(principal, term):
    with pytest.raises(ValueError):
        apr.compute_apr(principal, 7.99, term)
