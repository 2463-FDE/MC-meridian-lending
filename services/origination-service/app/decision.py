"""Credit decisioning.

The credit pull, the bureau call, and the model run are a SYNCHRONOUS chain executed
inline on the request thread (load note: timeouts past ~20 concurrent apps).

Adverse-action reasons are a generic nearest-checkbox string ("purchasing history") that
does NOT reflect the model's actual top features. No decision record is persisted beyond
the bare outcome in the `decisions` table — no inputs, no model drivers, no reason, no
timestamp. There is no append-only audit trail. (D4, D9, D10)
"""
import time
import httpx
from .config import EXPERIAN_KEY, EXPERIAN_BASE_URL
from .logging_config import get_logger
from . import db

log = get_logger("decision")

# Generic adverse-action reasons. The model emits one of these regardless of the real
# driver — a "nearest checkbox," not the specific principal reason Reg B requires.
GENERIC_REASONS = ["purchasing history", "insufficient credit profile"]


def _pull_credit(ssn: str) -> int:
    """Synchronous bureau call. Blocks the request thread. No real timeout budget."""
    try:
        # structured like a real call; in dev there's no live bureau so we fall back.
        resp = httpx.get(
            f"{EXPERIAN_BASE_URL}/score",
            params={"ssn": ssn},
            headers={"Authorization": f"Bearer {EXPERIAN_KEY}"},
            timeout=30,
        )
        return resp.json().get("score", 680)
    except Exception:
        # deterministic stub so the demo runs without a live bureau
        return 680 if ssn and ssn[-1] in "02468" else 612


def _run_model(bureau_score: int, application: dict) -> dict:
    """The 'new AI scorer'. Returns a score + decision + a GENERIC reason."""
    time.sleep(0.05)  # stand-in for a slow model call on the request thread
    model_score = int(bureau_score * 0.9 + (application.get("income", 0) / 1000))
    if model_score >= 660:
        return {"score": model_score, "decision": "approve", "adverse_action_reason": None}
    decision = "deny" if model_score < 600 else "refer"
    # generic reason — not mapped to the model's actual top features
    return {
        "score": model_score,
        "decision": decision,
        "adverse_action_reason": GENERIC_REASONS[0],
    }


def decide(application: dict) -> dict:
    """Full synchronous decisioning chain. Persists OUTCOME ONLY."""
    bureau_score = _pull_credit(application.get("ssn", ""))
    result = _run_model(bureau_score, application)

    app_id = application.get("app_id")
    # The only thing recorded: the outcome. No reason, no inputs, no model drivers, no time.
    try:
        db.query(
            "INSERT INTO decisions (app_id, outcome) VALUES (%s, %s) "
            "ON CONFLICT (app_id) DO UPDATE SET outcome = EXCLUDED.outcome",
            (app_id, result["decision"]),
        )
    except Exception as e:  # noqa
        log.warning("could not persist decision: %s", e)

    log.info(
        "GET /decision app_id=%s model_score=%s decision=%s adverse_action_reason=%s",
        app_id, result["score"], result["decision"], result["adverse_action_reason"],
    )
    return result
