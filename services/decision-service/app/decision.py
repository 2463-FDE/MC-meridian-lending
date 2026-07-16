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
from psycopg2 import errors as pg_errors

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


class DecisionInputMismatch(RuntimeError):
    """A request_id was reused for the same application but with different decision
    inputs (amount/income/term/debt/employment). Replaying the recorded decision would
    return a STALE outcome for changed data, so we fail closed with a conflict instead
    (PR #7 review). Maps to HTTP 409 at the router."""


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


def _has_negative_drivers(drivers: dict) -> bool:
    return any(a.get("contribution", 0) < 0 for a in drivers.get("attributions", []))


def _validate_record(
    outcome: str, band: str, principal_reasons: list, drivers: dict, decided_by: str
) -> None:
    """ADR 0008 requirement 2, enforced: no outcome without reasons+drivers; a system
    decision cannot contradict the policy band (the #6012 refer-band-deny class).

    Deny unconditionally requires reasons. Refer requires them only when the drivers
    include negative attributions — a borderline-band refer with uniformly non-negative
    contributions has no adverse reason to state and routes to manual review (ADR 0009
    §3 amendment; the original rule made that applicant class undecisionable)."""
    if outcome == "deny" and not principal_reasons:
        raise DecisionRecordError(
            "refusing deny: no specific principal reasons derived — an adverse "
            "action without recorded reasons violates the write-path contract"
        )
    if outcome == "refer" and not principal_reasons and _has_negative_drivers(drivers):
        raise DecisionRecordError(
            "refusing refer: negative model drivers exist but no principal reasons "
            "were derived — the record would hide the adverse drivers"
        )
    if not drivers:
        raise DecisionRecordError("refusing decision: no model drivers to record")
    if outcome != band and decided_by == model_vendor.model_signature():
        raise DecisionRecordError(
            f"refusing decision: system outcome '{outcome}' contradicts policy band "
            f"'{band}' — overrides require a human decided_by"
        )


_EVENT_COLUMNS = (
    "outcome, principal_reasons, drivers, policy_band, decided_by, request_id, inputs"
)


def _request_inputs(application: dict) -> dict:
    """The request-supplied decision inputs, built with the SAME defaults as the
    persisted inputs dict so a faithful replay compares equal. Excludes bureau_score (a
    pull result, not a request field) and SSN (never persisted — identifier-free)."""
    return {
        "annual_income": application.get("income", 0),
        "requested_amount": application.get("amount", 0),
        "term_months": application.get("term_months", 36),
        "monthly_debt": application.get("monthly_debt", 0),
        "employment_years": application.get("employment_years", 0),
    }


def _inputs_conflict(stored: dict | None, current: dict) -> bool:
    """True when a reused request_id arrives with decision inputs that differ from the
    recorded request. Money is float throughout this system, so compare as floats.
    Absent stored inputs (theoretical: request_id and inputs were co-added, so any keyed
    event has them) skip the check rather than false-reject a legitimate replay."""
    if not stored:
        return False
    return any(float(stored.get(k) or 0) != float(current[k] or 0) for k in current)


def _result_from_event(row: dict) -> dict:
    """Rebuild the decide() response from a persisted event row (idempotent replay)."""
    drivers = row.get("drivers") or {}
    return {
        "score": drivers.get("model_score"),
        "decision": row["outcome"],
        "policy_band": row.get("policy_band"),
        "principal_reasons": row.get("principal_reasons") or [],
        "decided_by": row.get("decided_by"),
    }


def _find_event_by_request(app_id: int, request_id: str):
    """Replay lookup is scoped to (app_id, request_id): a request_id reused on a
    different application never replays another application's record (PR #7 review)."""
    rows = db.query(
        f"SELECT {_EVENT_COLUMNS} FROM decision_events"
        " WHERE app_id = %s AND request_id = %s",
        (app_id, request_id),
    )
    return rows[0] if rows else None


def _persist_event(
    app_id: int,
    outcome: str,
    principal_reasons: list,
    drivers: dict,
    band: str,
    inputs: dict,
    decided_by: str,
    request_id: str | None,
) -> dict | None:
    """Append the decision event and update the current-state pointer in ONE atomic
    statement. Persistence failure refuses the decision (fail closed) — the record IS
    the deliverable, not a best-effort side effect.

    Returns None on a fresh insert. When a request_id collides with an already-
    persisted event (a concurrent retry losing the race), returns that existing row —
    the caller replays it instead of failing, so one officer action can never yield
    two regulated events."""
    try:
        db.query(
            "WITH ev AS ("
            "  INSERT INTO decision_events"
            "    (app_id, outcome, principal_reasons, drivers, policy_band, inputs,"
            "     decided_by, request_id)"
            "  VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s, %s)"
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
                request_id,
            ),
        )
        return None
    except pg_errors.UniqueViolation:
        # Concurrent duplicate of the same request: the first writer won; serve its
        # record (idempotent), never a second event.
        existing = _find_event_by_request(app_id, request_id) if request_id else None
        if existing is not None:
            log.info(
                "duplicate decision request replayed app_id=%s request_id=%s",
                app_id,
                request_id,
            )
            return existing
        raise DecisionRecordError(
            "decision refused: duplicate event conflict without a retrievable record"
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

    Idempotency (PR #7 review): when the caller supplies a request_id and an event
    with that id already exists FOR THE SAME application, the recorded decision is
    replayed — no bureau pull, no new event. A request WITHOUT a request_id is an
    explicit re-decision. The key is scoped per application: reused on a different
    app_id it is an independent key, never a replay of another application's record.
    """
    request_id = application.get("request_id") or None
    if request_id:
        existing = _find_event_by_request(application.get("app_id"), request_id)
        if existing is not None:
            if _inputs_conflict(existing.get("inputs"), _request_inputs(application)):
                log.warning(
                    "request_id reused with changed inputs app_id=%s request_id=%s — "
                    "refusing to replay a stale decision",
                    application.get("app_id"),
                    request_id,
                )
                raise DecisionInputMismatch(
                    "request_id reused with different decision inputs"
                )
            log.info(
                "decision replayed from record app_id=%s request_id=%s",
                application.get("app_id"),
                request_id,
            )
            return _result_from_event(existing)

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
    # The mapper validates the model's WHOLE vocabulary (fail closed on any unmapped
    # feature) on every outcome — approvals included (ADR 0009 §3 amendment). Adverse
    # reasons are only attached to non-approve outcomes.
    mapped_reasons = reasons.principal_reasons(model_out["attributions"])
    principal_reasons = [] if outcome == "approve" else mapped_reasons
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
    raced = _persist_event(
        app_id,
        outcome,
        principal_reasons,
        drivers,
        band,
        inputs,
        decided_by,
        request_id,
    )
    if raced is not None:
        # Lost the insert race to a concurrent request that reused this key. If that
        # winner decisioned DIFFERENT inputs, the two requests are not the same logical
        # action — fail closed with a conflict rather than serve a stale outcome.
        if _inputs_conflict(raced.get("inputs"), _request_inputs(application)):
            raise DecisionInputMismatch(
                "request_id reused with different decision inputs"
            )
        return _result_from_event(raced)

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
