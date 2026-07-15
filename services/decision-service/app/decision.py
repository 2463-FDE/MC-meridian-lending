"""Credit decisioning.

The credit pull and the model run remain a SYNCHRONOUS chain executed inline on the
request thread (load note: timeouts past ~20 concurrent apps — documented and deferred
in ADR 0009 §6).

Scoring runs through the vendor-model stand-in (model_vendor, ADR 0009 §2). Adverse-
action reasons are specific principal reasons derived from the model's actual top
negative attributions (reasons.py, ADR 0009 §3). Every decision persists an append-only
decision_events record atomically with the outcome; a decision that cannot be recorded
is refused (fail closed), because an unprovable decision is a Reg B liability, not a
success (ADR 0009 §4).
"""

import json

import httpx
from . import config
from . import model_vendor
from . import reasons
from .logging_config import get_logger
from . import db

log = get_logger("decision")


class CreditPullError(RuntimeError):
    """The bureau credit pull could not be completed. Raised so decisioning FAILS
    CLOSED — no decision is issued off a synthetic score in a production-like
    (non-synthetic) configuration."""


class DecisionRecordError(RuntimeError):
    """The decision-event record could not be validated or persisted. Raised so
    decisioning FAILS CLOSED — no outcome is issued without its Reg B record
    (ADR 0008 requirement 2, enforced here)."""


def _synthetic_score(ssn: str) -> int:
    """Deterministic demo score — ONLY used when synthetic credit is enabled
    (ENVIRONMENT=development AND ALLOW_SYNTHETIC_CREDIT; see config)."""
    return 680 if ssn and ssn[-1] in "02468" else 612


def _pull_credit(ssn: str) -> int:
    """Synchronous bureau call. Blocks the request thread. No real timeout budget.

    Fails CLOSED: with no EXPERIAN_KEY (or on any bureau failure) it raises
    CreditPullError unless synthetic credit is explicitly enabled for a dev
    environment (config.synthetic_credit_enabled — two gates). This prevents a
    keyless or production deployment from silently issuing decisions off a
    synthetic score.
    """
    if not config.EXPERIAN_KEY:
        if config.synthetic_credit_enabled():
            return _synthetic_score(ssn)
        raise CreditPullError(
            "EXPERIAN_KEY not configured — refusing to issue a decision without a "
            "real credit pull. Synthetic scoring requires ENVIRONMENT=development "
            "and ALLOW_SYNTHETIC_CREDIT (local/demo only)."
        )
    try:
        resp = httpx.get(
            f"{config.EXPERIAN_BASE_URL}/score",
            params={"ssn": ssn},
            headers={"Authorization": f"Bearer {config.EXPERIAN_KEY}"},
            timeout=30,
        )
        resp.raise_for_status()  # a bad/expired key returns 401/403 — treat as failure
        score = resp.json().get("score")
        if score is None:
            raise ValueError("bureau response missing 'score'")
        return score
    except Exception as e:
        if config.synthetic_credit_enabled():
            return _synthetic_score(ssn)
        # Fail closed — do NOT fall back to a stub in a real environment.
        raise CreditPullError(f"bureau credit pull failed: {type(e).__name__}") from e


def _validate_record(
    outcome: str, band: str, principal_reasons: list, drivers: dict, decided_by: str
) -> None:
    """ADR 0008 requirement 2, enforced: no outcome without reasons+drivers; a system
    decision cannot contradict the policy band (the #6012 refer-band-deny class)."""
    if outcome in ("deny", "refer") and not principal_reasons:
        raise DecisionRecordError(
            f"refusing {outcome}: no specific principal reasons derived — an adverse "
            "action without recorded reasons violates the write-path contract"
        )
    if not drivers:
        raise DecisionRecordError("refusing decision: no model drivers to record")
    if outcome != band and decided_by == model_vendor.model_signature():
        raise DecisionRecordError(
            f"refusing decision: system outcome '{outcome}' contradicts policy band "
            f"'{band}' — overrides require a human decided_by"
        )


def _persist_event(
    app_id: int,
    outcome: str,
    principal_reasons: list,
    drivers: dict,
    band: str,
    inputs: dict,
    decided_by: str,
) -> None:
    """Append the decision event and update the current-state pointer in ONE atomic
    statement. Persistence failure refuses the decision (fail closed) — the record IS
    the deliverable, not a best-effort side effect."""
    try:
        db.query(
            "WITH ev AS ("
            "  INSERT INTO decision_events"
            "    (app_id, outcome, principal_reasons, drivers, policy_band, inputs, decided_by)"
            "  VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s)"
            "  RETURNING app_id, outcome"
            ") "
            "INSERT INTO decisions (app_id, outcome) SELECT app_id, outcome FROM ev "
            "ON CONFLICT (app_id) DO UPDATE SET outcome = EXCLUDED.outcome",
            (
                app_id,
                outcome,
                json.dumps(principal_reasons),
                json.dumps(drivers),
                band,
                json.dumps(inputs),
                decided_by,
            ),
        )
    except Exception as e:
        log.error(
            "decision event persist failed for app_id=%s: %s", app_id, type(e).__name__
        )
        raise DecisionRecordError(
            "decision refused: event record could not be persisted"
        ) from e


def decide(application: dict) -> dict:
    """Full synchronous decisioning chain.

    Persists an append-only decision_events row (inputs, model outputs, reason codes)
    atomically with the outcome, or refuses the decision.
    """
    bureau_score = _pull_credit(application.get("ssn", ""))

    # Identifier-free model inputs (ADR 0007 rule 1) — this dict is persisted verbatim.
    inputs = {
        "bureau_score": bureau_score,
        "annual_income": application.get("income", 0),
        "requested_amount": application.get("amount", 0),
        "term_months": application.get("term_months", 36),
        "monthly_debt": application.get("monthly_debt", 0),
        "employment_years": application.get("employment_years", 0),
    }
    model_out = model_vendor.score_application(inputs)
    band = model_vendor.policy_band(model_out["score"])
    outcome = band  # system decisions follow the band; overrides are human-only
    principal_reasons = (
        []
        if outcome == "approve"
        else reasons.principal_reasons(model_out["attributions"])
    )
    drivers = {
        "model_id": model_out["model_id"],
        "model_version": model_out["model_version"],
        "model_score": model_out["score"],
        "attributions": model_out["attributions"],
        "band_cutoffs": {
            "approve": model_vendor.APPROVE_CUTOFF,
            "deny": model_vendor.DENY_CUTOFF,
        },
    }
    decided_by = model_vendor.model_signature()

    _validate_record(outcome, band, principal_reasons, drivers, decided_by)

    app_id = application.get("app_id")
    _persist_event(
        app_id, outcome, principal_reasons, drivers, band, inputs, decided_by
    )

    log.info(
        "decision recorded app_id=%s model_score=%s outcome=%s band=%s reasons=%s",
        app_id,
        model_out["score"],
        outcome,
        band,
        [r["code"] for r in principal_reasons],
    )
    return {
        "score": model_out["score"],
        "decision": outcome,
        "policy_band": band,
        "principal_reasons": principal_reasons,
        "decided_by": decided_by,
    }
