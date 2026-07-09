"""Credit decisioning.

This logic was lifted verbatim out of the origination service into its own
decision-service — the behaviour (and the debt) is unchanged by the split.

The credit pull, the bureau call, and the model run are a SYNCHRONOUS chain executed
inline on the request thread (load note: timeouts past ~20 concurrent apps).

Adverse-action reasons are a generic nearest-checkbox string ("purchasing history") that
does NOT reflect the model's actual top features. No decision record is persisted beyond
the bare outcome in the `decisions` table — no inputs, no model drivers, no reason, no
timestamp. There is no append-only audit trail. (D4, D9, D10)
"""
import time
import httpx
from . import config
from .logging_config import get_logger
from . import db

log = get_logger("decision")

# Generic adverse-action reasons. The model emits one of these regardless of the real
# driver — a "nearest checkbox," not the specific principal reason Reg B requires.
GENERIC_REASONS = ["purchasing history", "insufficient credit profile"]


class CreditPullError(RuntimeError):
    """The bureau credit pull could not be completed. Raised so decisioning FAILS
    CLOSED — no decision is issued off a synthetic score in a production-like
    (non-synthetic) configuration."""


def _synthetic_score(ssn: str) -> int:
    """Deterministic demo score — ONLY used when ALLOW_SYNTHETIC_CREDIT is set."""
    return 680 if ssn and ssn[-1] in "02468" else 612


def _pull_credit(ssn: str) -> int:
    """Synchronous bureau call. Blocks the request thread. No real timeout budget.

    Fails CLOSED: with no EXPERIAN_KEY (or on any bureau failure) it raises
    CreditPullError unless ALLOW_SYNTHETIC_CREDIT explicitly opts into the demo
    stub. This prevents a keyless deployment from silently issuing decisions off a
    synthetic score.
    """
    if not config.EXPERIAN_KEY:
        if config.ALLOW_SYNTHETIC_CREDIT:
            return _synthetic_score(ssn)
        raise CreditPullError(
            "EXPERIAN_KEY not configured — refusing to issue a decision without a "
            "real credit pull. Set ALLOW_SYNTHETIC_CREDIT=1 for local/demo only."
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
        if config.ALLOW_SYNTHETIC_CREDIT:
            return _synthetic_score(ssn)
        # Fail closed — do NOT fall back to a stub in a real environment.
        raise CreditPullError(f"bureau credit pull failed: {type(e).__name__}") from e


def _run_model(bureau_score: int, application: dict) -> dict:
    """The rules-based risk scorecard. Returns a score + decision + a GENERIC reason.

    (This is the legacy statistical scorecard. The client keeps asking for a smarter
    "AI" model — that work has not started; there is no ML/LLM in the baseline.)
    """
    time.sleep(0.05)  # stand-in for a slow scorecard pass on the request thread
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
