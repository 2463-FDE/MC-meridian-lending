"""Feature → adverse-action reason mapping tests (ADR 0009 §3, spec D2)."""

import pytest

from app.reasons import MAX_REASONS, REASON_MAP, UnmappedFeatureError, principal_reasons


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
