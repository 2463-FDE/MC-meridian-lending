"""TILA test vectors — the blocking gate for disclosed money.

Why this file exists separately from test_apr.py: `apr.py` shipped an APR that was 4.5pp
wrong (~36x the Reg Z tolerance) on every loan for the life of the service, and nothing
caught it. The unit tests that existed compared the implementation against a re-derivation
of its own formula, and CI ran them under `continue-on-error` + `|| true`. Two independent
failures — a self-agreeing test, and a tolerated one.

This file answers both:

1. **Expectations are pinned literals**, produced by an independent Newton solve at 50-digit
   precision (a different algorithm from apr.py's bisection). A change in the implementation
   cannot silently move them, because nothing here recomputes them from apr.py.
2. **It runs in its own BLOCKING CI job** (`tila-vectors-gate`), never the `backend` matrix.
   A money gate that inherits `|| true` is not a gate.

`test_pinned_expectations_satisfy_the_actuarial_identity` guards the literals themselves:
it checks the actuarial definition directly (present value of the payment stream at the
pinned APR equals the amount financed) without calling apr.py at all. If a vector is ever
mis-transcribed, that test fails even if the implementation happens to agree with it.

Adding a vector: compute it independently — do not paste output from `compute_apr`.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, getcontext

import pytest

from app import apr, rules

getcontext().prec = 50

CENT = Decimal("0.01")
TOLERANCE = Decimal(str(rules.load_fee_schedule().apr_tolerance_pp))


@dataclass(frozen=True)
class Vector:
    principal: str
    rate_pct: str
    term_months: int
    fee_pct: str
    apr: str
    finance_charge: str
    amount_financed: str
    monthly_payment: str


# Varying principal, note rate, term, and fee — including the boundaries that the add-on
# method got most wrong (short terms, high rates) and the two fee edges (0%, 5%).
VECTORS = [
    Vector(
        "18000",
        "7.99",
        48,
        "0.030",
        apr="9.584",
        finance_charge="3628.71",
        amount_financed="17460.00",
        monthly_payment="439.35",
    ),
    Vector(
        "5000",
        "12.5",
        24,
        "0.030",
        apr="15.596",
        finance_charge="826.88",
        amount_financed="4850.00",
        monthly_payment="236.54",
    ),
    Vector(
        "25000",
        "24.99",
        60,
        "0.030",
        apr="26.527",
        finance_charge="19768.19",
        amount_financed="24250.00",
        monthly_payment="733.64",
    ),
    Vector(
        "3000",
        "7.99",
        12,
        "0.030",
        apr="13.760",
        finance_charge="221.42",
        amount_financed="2910.00",
        monthly_payment="260.95",
    ),
    Vector(
        "40000",
        "15.0",
        84,
        "0.030",
        apr="16.055",
        finance_charge="26037.10",
        amount_financed="38800.00",
        monthly_payment="771.87",
    ),
    Vector(
        "10000",
        "9.99",
        36,
        "0.000",
        apr="9.990",
        finance_charge="1614.50",
        amount_financed="10000.00",
        monthly_payment="322.62",
    ),
    Vector(
        "10000",
        "9.99",
        36,
        "0.050",
        apr="13.552",
        finance_charge="2114.50",
        amount_financed="9500.00",
        monthly_payment="322.62",
    ),
    Vector(
        "12000",
        "0",
        24,
        "0.030",
        apr="2.941",
        finance_charge="360.00",
        amount_financed="11640.00",
        monthly_payment="500.00",
    ),
    Vector(
        "7500",
        "18.75",
        30,
        "0.025",
        apr="20.913",
        finance_charge="2139.53",
        amount_financed="7312.50",
        monthly_payment="315.07",
    ),
]

IDS = [f"{v.principal}@{v.rate_pct}%x{v.term_months}m,fee{v.fee_pct}" for v in VECTORS]


@pytest.fixture
def fee_pct(monkeypatch):
    """Override the origination fee for a vector without touching the policy file."""

    def _apply(pct: str):
        schedule = rules.load_fee_schedule()
        overridden = type(schedule)(
            **{**vars(schedule), "origination_fee_pct": float(pct)}
        )
        monkeypatch.setattr(rules, "get_fee_schedule", lambda: overridden)

    return _apply


@pytest.mark.parametrize("v", VECTORS, ids=IDS)
def test_disclosed_apr_within_tolerance_of_vector(v, fee_pct):
    fee_pct(v.fee_pct)
    disclosed = apr.compute_apr(v.principal, v.rate_pct, v.term_months)
    assert abs(disclosed - Decimal(v.apr)) <= TOLERANCE, (
        f"disclosed {disclosed} vs vector {v.apr} exceeds {TOLERANCE}pp"
    )


@pytest.mark.parametrize("v", VECTORS, ids=IDS)
def test_disclosed_finance_charge_matches_vector_to_the_cent(v, fee_pct):
    """The finance charge has no tolerance band the way the APR does — it is a dollar
    figure the borrower is owed accurately, so this asserts exact cents."""
    fee_pct(v.fee_pct)
    assert apr.finance_charge(v.principal, v.rate_pct, v.term_months) == Decimal(
        v.finance_charge
    )


@pytest.mark.parametrize("v", VECTORS, ids=IDS)
def test_amount_financed_and_payment_match_vector(v, fee_pct):
    fee_pct(v.fee_pct)
    assert apr.amount_financed(v.principal) == Decimal(v.amount_financed)
    payment = apr.monthly_payment(v.principal, v.rate_pct, v.term_months)
    assert payment.quantize(CENT, rounding=ROUND_HALF_UP) == Decimal(v.monthly_payment)


def _level_payment(principal: Decimal, rate_pct: Decimal, n: int) -> Decimal:
    """Unrounded annuity payment. Elementary formula, deliberately not apr.py's."""
    r = rate_pct / 100 / 12
    if r == 0:
        return principal / n
    factor = (1 + r) ** n
    return principal * r * factor / (factor - 1)


def _present_value(payment: Decimal, monthly_rate: Decimal, n: int) -> Decimal:
    if monthly_rate == 0:
        return payment * n
    return payment * (1 - (1 + monthly_rate) ** -n) / monthly_rate


@pytest.mark.parametrize("v", VECTORS, ids=IDS)
def test_pinned_apr_brackets_the_true_actuarial_root(v):
    """Guards the vectors themselves — does not call apr.py.

    A 3-decimal APR is a rounded value, so the actuarial identity cannot hold exactly: at
    the pinned rate the present value of the payment stream misses the amount financed by
    the rounding. The exact statement is a bracket — if `apr` is the correctly rounded
    root, then the true root lies within ±0.0005pp, so PV at (apr + 0.0005) must undershoot
    the amount financed and PV at (apr - 0.0005) must overshoot it. PV is monotonically
    decreasing in the rate, so straddling proves the root is inside the interval.

    A mis-transcribed literal fails here even if the implementation happens to agree with
    it, because nothing in this test comes from apr.py.
    """
    principal = Decimal(v.principal)
    financed = Decimal(v.amount_financed)
    n = v.term_months
    payment = _level_payment(principal, Decimal(v.rate_pct), n)

    half_ulp = Decimal("0.0005")
    pv_at_upper = _present_value(payment, (Decimal(v.apr) + half_ulp) / 100 / 12, n)
    pv_at_lower = _present_value(payment, (Decimal(v.apr) - half_ulp) / 100 / 12, n)

    assert pv_at_upper <= financed <= pv_at_lower, (
        f"vector {v} is internally inconsistent: amount financed {financed} is outside "
        f"[{pv_at_upper}, {pv_at_lower}] — the pinned APR is not the correctly rounded root"
    )


@pytest.mark.parametrize("v", VECTORS, ids=IDS)
def test_pinned_amount_financed_equals_principal_less_the_fee(v):
    """The other half of the vector's internal consistency, also without apr.py."""
    principal = Decimal(v.principal)
    fee = (principal * Decimal(v.fee_pct)).quantize(CENT, rounding=ROUND_HALF_UP)
    assert Decimal(v.amount_financed) == principal - fee


@pytest.mark.parametrize("v", VECTORS, ids=IDS)
def test_apr_relates_to_the_note_rate_as_the_fee_dictates(v):
    """Above the note rate when a fee is withheld; equal to it when none is.

    The add-on method printed 5.041% on a 7.99% note — this is the invariant that makes
    that class of error impossible to ship again.
    """
    note_rate = Decimal(v.rate_pct)
    disclosed = Decimal(v.apr)
    if Decimal(v.fee_pct) > 0:
        assert disclosed > note_rate
    else:
        assert disclosed == note_rate


def test_the_add_on_method_would_fail_this_gate():
    """Regression marker: the shipped-for-months value must not pass.

    5.041% was `compute_apr(18000, 7.99, 48)` before the fix. Asserting it fails the gate
    documents what this file is for, and fails loudly if someone reintroduces the formula.
    """
    add_on_result = Decimal("5.041")
    vector = next(v for v in VECTORS if v.principal == "18000")
    assert abs(add_on_result - Decimal(vector.apr)) > TOLERANCE
