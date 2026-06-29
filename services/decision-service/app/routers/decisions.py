"""Credit decisioning endpoint.

Runs the SYNCHRONOUS decisioning chain inline on the request thread and persists the
bare outcome only. No reason codes, no model drivers, no inputs, no timestamp are stored
(the missing decision audit trail). (D4, D9, D10)
"""
from fastapi import APIRouter

from .. import decision
from ..logging_config import get_logger
from ..schemas import DecisionIn, DecisionOut

log = get_logger("decisions")
router = APIRouter(prefix="/decisions", tags=["decisions"])


@router.post("", response_model=DecisionOut)
def run_decision(body: DecisionIn):
    payload = body.model_dump()
    # map the request onto the dict the legacy scorecard expects
    application = {
        "app_id": payload["application_id"],
        "ssn": payload.get("ssn") or "",
        "income": payload.get("annual_income") or 0,
    }
    result = decision.decide(application)  # synchronous chain; persists outcome only (debt)
    return DecisionOut(
        application_id=payload["application_id"],
        outcome=result["decision"],
        score=result["score"],
        reason=result.get("adverse_action_reason"),
    )
