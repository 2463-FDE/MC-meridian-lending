"""Feature → adverse-action reason mapping tests (ADR 0009 §3, spec D2)."""

import pytest

from app.reasons import (
    APPLICANT_STATED_FEATURES,
    CREDIT_DERIVED_FEATURES,
    MAX_REASONS,
    REASON_MAP,
    UnmappedFeatureError,
    feature_provenance,
    principal_reasons,
)


def test_top_negative_attributions_become_specific_reasons_most_negative_first():
    attributions = [
        {"feature": "payment_burden", "contribution": -80.0},
        {"feature": "income_sufficiency", "contribution": -20.0},
        {"feature": "delinquency_history", "contribution": 10.0},
        {"feature": "employment_tenure", "contribution": 2.0},
    ]
    reasons = principal_reasons(attributions)
    assert [r["code"] for r in reasons] == ["R02", "R03"]
    assert reasons[0]["reason"] == "Excessive obligations in relation to income"


def test_no_negative_attributions_yields_no_reasons():
    assert (
        principal_reasons([{"feature": "delinquency_history", "contribution": 20.0}])
        == []
    )


def test_unmapped_feature_fails_closed_even_when_positive():
    with pytest.raises(UnmappedFeatureError):
        principal_reasons([{"feature": "zodiac_sign", "contribution": 5.0}])


def test_reasons_capped_at_reg_b_maximum():
    attributions = [
        {"feature": f, "contribution": -float(i + 1)} for i, f in enumerate(REASON_MAP)
    ]
    assert len(principal_reasons(attributions)) <= MAX_REASONS


def test_generic_purchasing_history_is_not_in_the_vocabulary():
    texts = [text.lower() for _, text in REASON_MAP.values()]
    assert not any("purchasing history" in t for t in texts)


ALL_FEATURES = [
    {"feature": "delinquency_history", "contribution": 10.0},
    {"feature": "payment_burden", "contribution": -5.0},
    {"feature": "income_sufficiency", "contribution": -3.0},
    {"feature": "employment_tenure", "contribution": 1.0},
]


def test_feature_provenance_claims_bureau_verified_only_for_a_real_bureau_pull():
    assert feature_provenance(ALL_FEATURES, "bureau") == {
        "delinquency_history": "bureau_verified",
        "payment_burden": "applicant_stated",
        "income_sufficiency": "applicant_stated",
        "employment_tenure": "applicant_stated",
    }


def test_feature_provenance_labels_a_synthetic_score_synthetic():
    """D26 review: the accepted demo path enables ALLOW_SYNTHETIC_CREDIT, so a run
    that no bureau ever saw must not be recorded as bureau_verified."""
    assert feature_provenance(ALL_FEATURES, "synthetic") == {
        "delinquency_history": "synthetic_credit",
        "payment_burden": "applicant_stated",
        "income_sufficiency": "applicant_stated",
        "employment_tenure": "applicant_stated",
    }


def test_feature_provenance_distinguishes_a_failed_bureau_fallback():
    provenance = feature_provenance(ALL_FEATURES, "synthetic_after_bureau_failure")
    assert provenance["delinquency_history"] == "synthetic_credit_bureau_failed"


def test_feature_provenance_never_claims_bureau_verified_off_a_synthetic_source():
    for source in ("synthetic", "synthetic_after_bureau_failure"):
        provenance = feature_provenance(ALL_FEATURES, source)
        assert "bureau_verified" not in provenance.values(), source


def test_feature_provenance_fails_closed_on_unmapped_feature():
    with pytest.raises(UnmappedFeatureError):
        feature_provenance([{"feature": "zodiac_sign", "contribution": 5.0}], "bureau")


def test_feature_provenance_fails_closed_on_an_unrecognised_credit_source():
    """An unclassifiable score origin must refuse the decision, not guess a label."""
    with pytest.raises(UnmappedFeatureError):
        feature_provenance(ALL_FEATURES, "cousin_vinny")


def test_reason_and_provenance_vocabularies_cannot_drift():
    """D26 review: the runtime fail-closed guards only fire on a live decision, so
    without this CI never catches a feature added to one mapping and not the other."""
    assert REASON_MAP.keys() == CREDIT_DERIVED_FEATURES | APPLICANT_STATED_FEATURES
    assert not CREDIT_DERIVED_FEATURES & APPLICANT_STATED_FEATURES
