"""Credit decisioning endpoint.

Runs the SYNCHRONOUS decisioning chain inline on the request thread (deferred async:
ADR 0009 §6) and persists an append-only decision_events record atomically with the
outcome — or refuses the decision (fail closed, ADR 0009 §4).
"""

from fastapi import APIRouter, HTTPException, Query

from .. import db, decision
from ..logging_config import get_logger
from ..reasons import UnmappedFeatureError
from ..schemas import DecisionIn, DecisionOut, DecisionRecordOut

log = get_logger("decisions")
router = APIRouter(prefix="/decisions", tags=["decisions"])


@router.get("/{app_id}/record", response_model=DecisionRecordOut)
def get_decision_record(
    app_id: int,
    request_id: str | None = Query(default=None, max_length=64),
):
    """Decision event for an application — the identifier-free projection the officer
    assistant's memory tool reads (ADR 0009 §5). Legacy outcomes whose reasons were
    never captured answer status=no_record_legacy, distinct from 404.

    When request_id is supplied the lookup is scoped to that exact event (the one the
    caller's own decision created) instead of the app's latest, so a concurrent
    re-decision landing between scoring and validation cannot swap the record out from
    under the request (PR #7 review). A scoped miss is a 404 — legacy rows carry no
    request_id, so falling back to app-latest would return an unrelated event."""
    if request_id is not None:
        events = db.query(
            "SELECT outcome, principal_reasons, drivers, policy_band, inputs, "
            "decided_by, decided_at FROM decision_events "
            "WHERE app_id = %s AND request_id = %s ORDER BY id DESC LIMIT 1",
            (app_id, request_id),
        )
        if not events:
            raise HTTPException(
                status_code=404, detail="no decision event for this request_id"
            )
    else:
        events = db.query(
            "SELECT outcome, principal_reasons, drivers, policy_band, inputs, "
            "decided_by, decided_at FROM decision_events "
            "WHERE app_id = %s ORDER BY id DESC LIMIT 1",
            (app_id,),
        )
    if events:
        e = events[0]
        return DecisionRecordOut(
            application_id=app_id,
            status="recorded",
            outcome=e["outcome"],
            principal_reasons=e["principal_reasons"],
            drivers=e["drivers"],
            policy_band=e["policy_band"],
            inputs=e["inputs"],
            decided_by=e["decided_by"],
            decided_at=e["decided_at"].isoformat() if e["decided_at"] else None,
        )
    legacy = db.query("SELECT outcome FROM decisions WHERE app_id = %s", (app_id,))
    if legacy:
        return DecisionRecordOut(
            application_id=app_id,
            status="no_record_legacy",
            outcome=legacy[0]["outcome"],
        )
    raise HTTPException(status_code=404, detail="application was never decisioned")


@router.post("", response_model=DecisionOut)
def run_decision(body: DecisionIn):
    payload = body.model_dump()
    application = {
        "app_id": payload["application_id"],
        "ssn": payload.get("ssn") or "",
        "income": payload.get("annual_income") or 0,
        "amount": payload.get("requested_amount") or 0,
        "term_months": payload.get("term_months") or 36,
        "monthly_debt": payload.get("monthly_debt") or 0,
        "employment_years": payload.get("employment_years") or 0,
        "request_id": payload.get("request_id"),
    }
    try:
        result = decision.decide(application)
    except decision.CreditPullError as e:
        # Fail closed: no decision is issued when the bureau pull cannot be made.
        log.error("credit pull unavailable, refusing decision: %s", e)
        raise HTTPException(status_code=503, detail="credit bureau unavailable") from e
    except decision.DecisionRecordError as e:
        # Fail closed: no decision is issued without its persisted Reg B record.
        log.error("decision record refused: %s", e)
        raise HTTPException(
            status_code=503, detail="decision could not be recorded"
        ) from e
    except UnmappedFeatureError as e:
        # Fail closed: the model's vocabulary cannot be explained (ADR 0009 §3 gate).
        # A typed refusal, not a 500 — the model integration is unfit, not the service.
        log.error("decision refused, unmapped model feature: %s", e)
        raise HTTPException(
            status_code=503,
            detail="decision refused: model feature has no mapped adverse-action reason",
        ) from e
    principal_reasons = result.get("principal_reasons") or []
    return DecisionOut(
        application_id=payload["application_id"],
        outcome=result["decision"],
        score=result["score"],
        reason=principal_reasons[0]["reason"] if principal_reasons else None,
        policy_band=result.get("policy_band"),
        principal_reasons=principal_reasons,
        decided_by=result.get("decided_by"),
    )
