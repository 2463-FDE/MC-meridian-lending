"""Request-validation tests for the application schema (these PASS)."""

from datetime import date

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
    "raw,normalized",
    [
        (" 412559980 ", "412559980"),
        (" 412-55-9980 ", "412-55-9980"),
        (" 412559980", "412559980"),
        ("412-55-9980 ", "412-55-9980"),
    ],
)
def test_ssn_padding_stripped_at_boundary(raw, normalized):
    # A padded-but-valid SSN matched _SSN_RE (checked against v.strip()) but the
    # validator returned the raw v, so " 412559980 " passed and model_dump()
    # preserved the padding -- forwarding/storing a malformed SSN. Normalize to
    # the stripped value so only a canonical SSN leaves the boundary.
    assert _app(ssn=raw).ssn == normalized


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


@pytest.mark.parametrize(
    "phone",
    [
        "12345",
        "55555501234",
        "not-a-phone",
        # Junk wrappers that carry exactly 10 digits and so slipped past the old
        # digit-count-only check, yet sit outside the labeled-phone redactor's NANP
        # shape -- once labeled they survive redaction into logs/payloads/storage
        # (PR review). The anchored allowlist must reject them.
        "abc5555550123",
        "5555550123xyz",
        "555::::123::::4567",
        "555/555/0123",
        "555_555_0123",
    ],
)
def test_phone_malformed_rejected(phone):
    with pytest.raises(ValidationError):
        _app(phone=phone)


@pytest.mark.parametrize(
    "raw,normalized",
    [
        (" 5555550123 ", "5555550123"),
        (" (555) 555-0123 ", "(555) 555-0123"),
        ("5555550123 ", "5555550123"),
    ],
)
def test_phone_padding_stripped_at_boundary(raw, normalized):
    # Same blindspot as the SSN validator: the digit-count check ignores surrounding
    # whitespace, so a padded phone passed and model_dump() preserved the padding.
    # Normalize to the stripped value; internal formatting is untouched.
    assert _app(phone=raw).phone == normalized


@pytest.mark.parametrize(
    "dob",
    ["1990-04-22", "1900-01-01", "2007-12-31"],
)
def test_dob_valid_dates_accepted(dob):
    assert _app(dob=dob).dob == dob


def test_dob_optional_when_absent():
    # Entity applicants carry an EIN and no DOB (_entity_requires_ein), so absent is valid.
    assert _app().dob is None


@pytest.mark.parametrize(
    "dob",
    [
        # THE DEFECT: a native date input's year spinner emits a 5-digit year. Postgres
        # DATE stores it (its ceiling is year 294276), then SQLAlchemy cannot build a
        # Python date from the row and GET /los/applications raises
        # "ValueError: year 21990 is out of range" -- taking down the officer queue for
        # every officer, over one bad row, not just the application carrying it.
        "21990-04-22",
        "990-04-22",  # 3-digit year, the same class from the other direction
        "1990-4-22",  # non-ISO shape the DATE column would otherwise coerce
        "1990-13-01",  # impossible month
        "1990-02-30",  # impossible day
        "not-a-date",
    ],
)
def test_dob_unparseable_rejected(dob):
    with pytest.raises(ValidationError):
        _app(dob=dob)


def test_dob_before_floor_year_rejected():
    with pytest.raises(ValidationError):
        _app(dob="1899-12-31")


def test_dob_in_the_future_rejected():
    future = date.today().replace(year=date.today().year + 1)
    with pytest.raises(ValidationError):
        _app(dob=future.isoformat())


def test_dob_padding_stripped_at_boundary():
    # Parity with the ssn/phone validators: date.fromisoformat rejects surrounding
    # whitespace outright, so strip before parsing and store the canonical value.
    assert _app(dob=" 1990-04-22 ").dob == "1990-04-22"


@pytest.mark.parametrize("dob", ["", "   ", "\t\n"])
def test_dob_blank_normalized_to_none(dob):
    # A blank DOB used to survive as "" and reach the applicants.dob DATE column, where
    # Postgres raises "invalid input syntax for type date" inside the intake transaction --
    # a 500 for a request the boundary claims to validate. Blank means absent (dob is
    # optional for entity applicants), so normalize to None and store SQL NULL.
    assert _app(dob=dob).dob is None


@pytest.mark.parametrize(
    "dob",
    [
        # date.fromisoformat is not a shape check. Python 3.11+ parses the ISO basic form
        # and week dates, and the validator returned the raw string, so these left the
        # boundary unnormalized: Postgres coerces "19900422" (storing a value that never
        # matched the promised shape) and rejects "2021-W01-1" with a 500.
        "19900422",
        "2021-W01-1",
        "1990-W01",
        "1990-04-22T00:00:00",
    ],
)
def test_dob_non_canonical_iso_rejected(dob):
    with pytest.raises(ValidationError):
        _app(dob=dob)
