"""Vendor model stub tests (ADR 0009 §2, spec D1)."""

from app import model_vendor
from app.reasons import REASON_MAP


STRONG = {
    "bureau_score": 680,
    "annual_income": 100000,
    "requested_amount": 15000,
    "term_months": 36,
    "monthly_debt": 0,
    "employment_years": 5,
}
WEAK = {
    "bureau_score": 612,
    "annual_income": 0,
    "requested_amount": 15000,
    "term_months": 36,
    "monthly_debt": 0,
    "employment_years": 0,
}


def test_deterministic_same_input_same_output():
    assert model_vendor.score_application(STRONG) == model_vendor.score_application(
        STRONG
    )


def test_output_shape_has_model_identity_and_ranked_attributions():
    out = model_vendor.score_application(STRONG)
    assert out["model_id"] == "meridian-risk-stub"
    assert out["model_version"] == "1"
    contributions = [a["contribution"] for a in out["attributions"]]
    assert contributions == sorted(contributions)  # most negative first


def test_every_emitted_feature_has_a_reason_mapping():
    # The fail-closed contract (ADR 0009 §3): stub may not grow a feature
    # without a mapped adverse-action reason.
    out = model_vendor.score_application(STRONG)
    for a in out["attributions"]:
        assert a["feature"] in REASON_MAP


def test_strong_applicant_lands_in_approve_band():
    out = model_vendor.score_application(STRONG)
    assert out["score"] >= model_vendor.APPROVE_CUTOFF
    assert model_vendor.policy_band(out["score"]) == "approve"


def test_weak_applicant_lands_in_deny_band():
    out = model_vendor.score_application(WEAK)
    assert out["score"] < model_vendor.DENY_CUTOFF
    assert model_vendor.policy_band(out["score"]) == "deny"


def test_refer_band_is_reachable():
    mid = dict(WEAK, annual_income=30000, employment_years=2)
    out = model_vendor.score_application(mid)
    assert model_vendor.DENY_CUTOFF <= out["score"] < model_vendor.APPROVE_CUTOFF
    assert model_vendor.policy_band(out["score"]) == "refer"


def test_different_drivers_rank_differently():
    # Zero income: payment burden dominates. Short tenure only: tenure dominates.
    no_income = model_vendor.score_application(WEAK)
    assert no_income["attributions"][0]["feature"] == "payment_burden"
    short_tenure = model_vendor.score_application(dict(STRONG, employment_years=0))
    negatives = [
        a["feature"] for a in short_tenure["attributions"] if a["contribution"] < 0
    ]
    assert negatives == ["employment_tenure"]


def test_policy_band_edges():
    assert model_vendor.policy_band(660) == "approve"
    assert model_vendor.policy_band(659) == "refer"
    assert model_vendor.policy_band(600) == "refer"
    assert model_vendor.policy_band(599) == "deny"
