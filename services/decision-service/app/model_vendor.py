"""Deterministic stand-in for the licensed vendor risk model ("meridian-risk-stub v1").

No licensed model artifact exists — this module emits the vendor output *shape* the
platform integrates against (ADR 0009 §2): a score, a model identity, and RANKED SIGNED
FEATURE ATTRIBUTIONS. A real vendor model replaces this module behind score_application()
without touching the write path; the reason-mapping fail-closed rule in reasons.py is its
integration gate.

Same input → same output. No randomness, no time, no network.
"""

MODEL_ID = "meridian-risk-stub"
MODEL_VERSION = "1"

_BASE_SCORE = 640

# Policy bands (policies/underwriting_guidelines.md): approve >= 660, refer 600-659, deny < 600.
APPROVE_CUTOFF = 660
DENY_CUTOFF = 600


def model_signature() -> str:
    return f"{MODEL_ID}:v{MODEL_VERSION}"


def _feature_contributions(inputs: dict) -> dict:
    """Signed score contributions per feature. Every key returned here MUST have a
    reason mapping in reasons.REASON_MAP — an unmapped feature refuses the decision
    (fail closed) rather than issuing one with a fallback reason."""
    bureau = float(inputs.get("bureau_score") or 0)
    income = float(inputs.get("annual_income") or 0)
    amount = float(inputs.get("requested_amount") or 0)
    term = int(inputs.get("term_months") or 36)
    monthly_debt = float(inputs.get("monthly_debt") or 0)
    employment_years = float(inputs.get("employment_years") or 0)

    monthly_income = income / 12.0
    new_payment = amount / term if term else 0.0
    if monthly_income > 0:
        dti = (monthly_debt + new_payment) / monthly_income
        payment_burden = max((0.35 - dti) * 120.0, -80.0)
    else:
        payment_burden = -80.0  # no income to service any payment

    income_ratio = income / amount if amount else 0.0

    return {
        "delinquency_history": (bureau - _BASE_SCORE) * 0.5,
        "payment_burden": payment_burden,
        "income_sufficiency": min(income_ratio, 5.0) * 8.0 - 20.0,
        "employment_tenure": min(employment_years, 10.0) * 2.0 - 8.0,
    }


def score_application(inputs: dict) -> dict:
    """Score an application. Returns the vendor output shape:

    {score, model_id, model_version, attributions: [{feature, contribution}, ...]}

    attributions are ranked most-negative first — the ordering the adverse-action
    reason mapping consumes.
    """
    contributions = _feature_contributions(inputs)
    score = int(round(_BASE_SCORE + sum(contributions.values())))
    attributions = [
        {"feature": name, "contribution": round(value, 2)}
        for name, value in sorted(contributions.items(), key=lambda kv: kv[1])
    ]
    return {
        "score": score,
        "model_id": MODEL_ID,
        "model_version": MODEL_VERSION,
        "attributions": attributions,
    }


def policy_band(score: int) -> str:
    if score >= APPROVE_CUTOFF:
        return "approve"
    if score >= DENY_CUTOFF:
        return "refer"
    return "deny"
