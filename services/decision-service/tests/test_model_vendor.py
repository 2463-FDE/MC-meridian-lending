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


# --- The model card's input claim, demonstrated ---------------------------------
#
# The model card states: a deterministic stand-in, six inputs, none of them a
# prohibited basis. The three tests below are what that sentence cites. "The model
# does not see race" is the first question an examiner asks, and an assertion in a
# document is not evidence — these read the model's actual behaviour.

SIX_MODEL_INPUTS = {
    "bureau_score",
    "annual_income",
    "requested_amount",
    "term_months",
    "monthly_debt",
    "employment_years",
}

# Every prohibited basis under Reg B (12 CFR 1002.2(z)), plus the direct identifiers
# an applicant record carries. None of these is a model input; the point of passing
# them is to prove the model ignores them even when handed them.
PROHIBITED_AND_IDENTIFYING = {
    "race": "asian",
    "color": "brown",
    "religion": "hindu",
    "national_origin": "IN",
    "sex": "female",
    "marital_status": "married",
    "age": 27,
    "public_assistance": True,
    "exercised_ccpa_rights": True,
    "name": "Maria Alvarez",
    "dob": "1998-04-22",
    "ssn": "123-45-6789",
    "address": "12 Elm St",
    "zip": "02139",
}


class _RecordingInputs(dict):
    """A dict that records every key the model actually reads.

    Demonstrates the six-input claim instead of restating it: a later change that
    starts reading a seventh key fails here even if the score is unchanged.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read: set[str] = set()

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.add(key)
        return super().__getitem__(key)


def test_scorecard_reads_exactly_the_six_declared_inputs():
    probe = _RecordingInputs(STRONG)
    model_vendor.score_application(probe)
    assert probe.read == SIX_MODEL_INPUTS


def test_scorecard_reads_no_prohibited_basis_even_when_handed_one():
    probe = _RecordingInputs({**STRONG, **PROHIBITED_AND_IDENTIFYING})
    model_vendor.score_application(probe)
    assert probe.read.isdisjoint(PROHIBITED_AND_IDENTIFYING)
    assert probe.read == SIX_MODEL_INPUTS


def test_prohibited_basis_cannot_change_the_score_or_the_reasons():
    # Same six values, every prohibited basis attached: identical score, identical
    # attributions, therefore identical adverse-action reasons.
    baseline = model_vendor.score_application(STRONG)
    with_identity = model_vendor.score_application(
        {**STRONG, **PROHIBITED_AND_IDENTIFYING}
    )
    assert with_identity == baseline

    # And on the denial path, where the reasons are what an applicant is told.
    denied = model_vendor.score_application(WEAK)
    denied_with_identity = model_vendor.score_application(
        {**WEAK, **PROHIBITED_AND_IDENTIFYING}
    )
    assert denied_with_identity == denied
