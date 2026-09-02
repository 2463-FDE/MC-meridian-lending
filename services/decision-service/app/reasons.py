"""Model feature → specific adverse-action reason mapping (ADR 0009 §3).

Reg B requires the adverse-action notice to state the specific principal reason(s) for
the action (12 CFR 1002.9: no AI exemption). Reasons here are derived from the
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

# D26: delinquency_history is the one feature derived from the credit score, so its
# provenance is whatever that score's REAL source was. Synthetic scoring is reachable on
# the accepted demo path (ALLOW_SYNTHETIC_CREDIT in docker-compose.demo.yml) and
# _pull_credit also falls back to it when a configured bureau call fails, so labelling
# delinquency_history bureau_verified unconditionally writes a false claim onto the
# decision record. The other three features are computed from applicant-typed income,
# debt and tenure that nothing in this platform verifies. Together these two sets must
# cover every feature model_vendor can emit, same rule as REASON_MAP.
CREDIT_DERIVED_FEATURES = frozenset({"delinquency_history"})
APPLICANT_STATED_FEATURES = frozenset(
    {"payment_burden", "income_sufficiency", "employment_tenure"}
)

# Provenance label per credit-score source, as returned by decision._pull_credit.
CREDIT_SOURCE_PROVENANCE = {
    "bureau": "bureau_verified",
    "synthetic": "synthetic_credit",
    "synthetic_after_bureau_failure": "synthetic_credit_bureau_failed",
}


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


def feature_provenance(attributions: list, credit_source: str) -> dict:
    """Per-feature data provenance for the decision record (D26 rung 1).

    credit_source is the real origin of the score (decision._pull_credit): a synthetic
    run is recorded as synthetic_credit, never as bureau_verified.

    Fails closed on an unmapped feature AND on an unrecognised credit source, same rule
    as principal_reasons — a feature or a score whose origin we cannot classify must not
    decide silently, and an unknown source must not be labelled by guesswork."""
    if credit_source not in CREDIT_SOURCE_PROVENANCE:
        raise UnmappedFeatureError(
            f"unrecognised credit-score source {credit_source!r} — refusing to issue a "
            "decision rather than record an unverifiable provenance claim (D26)"
        )
    classified = CREDIT_DERIVED_FEATURES | APPLICANT_STATED_FEATURES
    unmapped = [a["feature"] for a in attributions if a["feature"] not in classified]
    if unmapped:
        raise UnmappedFeatureError(
            f"model features with no provenance classification: {unmapped} — "
            "refusing to issue a decision (D26 fail-closed rule)"
        )
    return {
        a["feature"]: (
            CREDIT_SOURCE_PROVENANCE[credit_source]
            if a["feature"] in CREDIT_DERIVED_FEATURES
            else "applicant_stated"
        )
        for a in attributions
    }
