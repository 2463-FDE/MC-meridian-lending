"""Request-validation tests for the application schema (these PASS)."""

import pytest
from pydantic import ValidationError

from app.schemas import ApplicationIn


def test_valid_application():
    a = ApplicationIn(
        name="Test Borrower", amount=10000, term_months=36, monthly_debt=500
    )
    assert a.amount == 10000
    assert a.term_months == 36
    assert a.monthly_debt == 500


def test_amount_over_cap_rejected():
    with pytest.raises(ValidationError):
        ApplicationIn(name="Test", amount=75000, term_months=36, monthly_debt=0)


def test_term_out_of_range_rejected():
    with pytest.raises(ValidationError):
        ApplicationIn(name="Test", amount=10000, term_months=6, monthly_debt=0)


def test_name_required():
    with pytest.raises(ValidationError):
        ApplicationIn(name="", amount=10000, monthly_debt=0)


def test_monthly_debt_required():
    # PR #7 review: monthly_debt is a required underwriting input. Omitting it must be
    # rejected at the boundary, not silently scored as zero debt (over-approval risk).
    with pytest.raises(ValidationError):
        ApplicationIn(name="Test", amount=10000, term_months=36)
    # explicit 0 is a valid value (no debt), distinct from missing
    assert (
        ApplicationIn(
            name="Test", amount=10000, term_months=36, monthly_debt=0
        ).monthly_debt
        == 0
    )


def _app(**kw):
    return ApplicationIn(name="Test", amount=10000, monthly_debt=0, **kw)


@pytest.mark.parametrize("ssn", ["412-55-9980", "412559980"])
def test_ssn_valid_shapes_accepted(ssn):
    assert _app(ssn=ssn).ssn == ssn


@pytest.mark.parametrize(
    "ssn",
    [
        "412 55 9980",
        "999999999999999",
        "abc-de-fghi",
        "412.55.9980",
        "12-34-5678",
        # Partially-dashed shapes: exactly one of the two separators present. An
        # independently-optional-dash regex accepted these; the all-or-nothing
        # alternation must reject them (fix/redactor-ssn-separator-blindspots review).
        "412-559980",
        "41255-9980",
    ],
)
def test_ssn_malformed_rejected(ssn):
    # The redactor's separator handling (this branch) should never have to absorb these:
    # reject the shape at the boundary instead.
    with pytest.raises(ValidationError):
        _app(ssn=ssn)


def test_ssn_optional_when_absent():
    # Entity applicants carry an EIN, not an SSN; absent/blank stays valid.
    assert _app().ssn is None


@pytest.mark.parametrize("phone", ["(555) 555-0123", "555-555-0123", "5555550123"])
def test_phone_valid_shapes_accepted(phone):
    assert _app(phone=phone).phone == phone


@pytest.mark.parametrize("phone", ["12345", "55555501234", "not-a-phone"])
def test_phone_malformed_rejected(phone):
    with pytest.raises(ValidationError):
        _app(phone=phone)
