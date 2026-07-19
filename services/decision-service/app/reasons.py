"""Model feature → specific adverse-action reason mapping (ADR 0009 §3).

Reg B requires the adverse-action notice to state the specific principal reason(s) for
the action (CFPB Circular 2023-03: no AI exemption). Reasons here are derived from the
model's ACTUAL top negative attributions for the applicant — never a generic fallback.

Reason texts use adverse-action vocabulary and are subject to the open compliance/legal
review recorded in ADR 0009; the mapping *mechanism* is what is locked.
"""

# Locked in ADR 0009 §3. Keys must cover every feature model_vendor can emit.
REASON_MAP = {
    "delinquency_history": (
        "R01",
        "Delinquent past or present credit obligations with others",
    ),
    "payment_burden": ("R02", "Excessive obligations in relation to income"),
    "income_sufficiency": ("R03", "Income insufficient for amount of credit requested"),
    "employment_tenure": ("R04", "Length of employment"),
}

# Reg B custom: state up to four principal reasons.
MAX_REASONS = 4


class UnmappedFeatureError(RuntimeError):
    """The model emitted a feature with no adverse-action reason mapping. Fail closed:
    refuse the decision rather than issue one with a missing or fallback reason. This is
    the integration gate for any future real vendor model (ADR 0009 §3)."""


def principal_reasons(attributions: list) -> list:
    """Specific principal reasons from ranked signed attributions.

    Validates EVERY feature is mapped (fail closed on any unmapped feature, even a
    positive one — a model whose vocabulary we cannot explain must not decide), then
    returns the top negative contributors, most negative first, as
    [{code, reason, feature}, ...]. Empty list when nothing pulls the score down.
    """
    unmapped = [a["feature"] for a in attributions if a["feature"] not in REASON_MAP]
    if unmapped:
        raise UnmappedFeatureError(
            f"model features with no adverse-action reason mapping: {unmapped} — "
            "refusing to issue a decision (ADR 0009 fail-closed rule)"
        )
    negatives = sorted(
        (a for a in attributions if a["contribution"] < 0),
        key=lambda a: a["contribution"],
    )
    return [
        {
            "code": REASON_MAP[a["feature"]][0],
            "reason": REASON_MAP[a["feature"]][1],
            "feature": a["feature"],
        }
        for a in negatives[:MAX_REASONS]
    ]
