"""D2 fixture-set sanity checks (docs/specs/disclosure-narration-judge.md).

Not the judge itself (D3) — these just guard the pinned dataset's shape and uniqueness.
D2 is independently landable (spec's Minimum Build Slice): no import from D1's guard here,
so this suite does not depend on D1 having merged first.
"""

from app.disclosure_coordinator import FIGURE_FIELDS

from .fixtures.disclosure_narration_fixtures import NARRATION_FIXTURES

_VALID_ACTIONS = {"review_and_send", "hold_for_compliance", None}


def test_fixture_count_is_in_the_pinned_range():
    assert 12 <= len(NARRATION_FIXTURES) <= 15


def test_fixture_ids_are_unique():
    ids = [f["id"] for f in NARRATION_FIXTURES]
    assert len(ids) == len(set(ids))


def test_application_ids_are_synthetic_and_unique():
    app_ids = [f["application_id"] for f in NARRATION_FIXTURES]
    assert len(app_ids) == len(set(app_ids))
    assert all(app_id >= 990001 for app_id in app_ids), (
        "application_id must stay clear of db/init/002_seed.sql's demo rows"
    )


def test_term_and_rate_stay_within_the_enforced_schema_bounds():
    for fixture in NARRATION_FIXTURES:
        assert 12 <= fixture["term_months"] <= 60, fixture["id"]
        assert 0 < fixture["note_rate_pct"] <= 35, fixture["id"]


def test_checks_passed_is_non_negative():
    assert all(f["checks_passed"] >= 0 for f in NARRATION_FIXTURES)


def test_expected_officer_action_is_a_valid_enum_value_or_unset():
    assert all(
        f["expected_officer_action"] in _VALID_ACTIONS for f in NARRATION_FIXTURES
    )


def test_every_named_category_from_the_spec_is_represented():
    """The spec's D2 categories: normal, unusually long term, minimum term, mismatch.

    Top-of-band rate is deliberately not among them — the spec rules that axis not
    gradeable, so there is no such category to cover.
    """
    ids = {f["id"] for f in NARRATION_FIXTURES}
    assert any("normal" in i for i in ids)
    assert any("unusually_long_term" in i for i in ids)
    assert any("minimum_term" in i for i in ids)
    assert any("checks_passed" in i for i in ids)


def test_no_fixture_expects_hold_for_compliance():
    """Every schema-legal term is <= 60, and the spec's only cutoff is `term_months > 60`.

    Whichever fixture later trips this is either a schema change (term ceiling raised) or a
    fixture written against a cutoff that no longer exists.
    """
    assert all(
        f["expected_officer_action"] != "hold_for_compliance"
        for f in NARRATION_FIXTURES
    )


def test_rate_driven_fixtures_pin_no_officer_action():
    """The rate criterion is not graded.

    policies/fee_schedule.md documents a band (7.99%-24.99%), but no code enforces it --
    POLICY_RATE_PCT hardcodes every issued offer to 7.99%, so no fixture can be a real
    top-of-band offer.
    """
    rate_driven = [f for f in NARRATION_FIXTURES if f["id"].startswith("rate_")]
    assert rate_driven, "the groundedness axis still needs high-rate inputs"
    assert all(f["expected_officer_action"] is None for f in rate_driven)


def test_checks_passed_mismatch_fixtures_do_not_match_figure_fields_length():
    mismatched = [f for f in NARRATION_FIXTURES if f["id"].startswith("checks_passed")]
    assert mismatched, "at least one defensive checks_passed fixture must exist"
    assert all(f["checks_passed"] != len(FIGURE_FIELDS) for f in mismatched)
