"""Credit decisioning endpoint.

Runs the SYNCHRONOUS decisioning chain inline on the request thread (deferred async:
ADR 0009 §6) and persists an append-only decision_events record atomically with the
outcome — or refuses the decision (fail closed, ADR 0009 §4).
"""

from fastapi import APIRouter, HTTPException

from .. import decision
from ..logging_config import get_logger
from ..schemas import DecisionIn, DecisionOut

log = get_logger("decisions")
router = APIRouter(prefix="/decisions", tags=["decisions"])


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
