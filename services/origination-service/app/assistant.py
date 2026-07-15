"""Single-agent decisioning assistant (ADR 0009 §5, spec D4).

A deterministic loop drives the `decision_assistant` prompt through ClaudeClient:
each turn the model returns one schema-validated JSON action — call a tool, or give
the final answer. Code executes the tools; the model orchestrates and narrates.

Compliance posture:
- The regulated decision (and its append-only decision_events record) happens INSIDE
  the score tool, in decision-service — the model cannot decision an application
  without the record being written, and cannot supply applicant data (tools take only
  an application id; lookups happen in code).
- Tool results fed back to the model are identifier-free enum codes and numbers only,
  so the ADR 0005 history-redaction path passes them intact (fail closed on anything
  else).
- The model's final answer is VALIDATED against the persisted record before it
  reaches the officer: on any mismatch the recorded facts are returned, never the
  narration (trust the record, not the model).
"""

import json


from . import clients
from .logging_config import get_logger
from .routers.applications import decision_request_payload

log = get_logger("assistant")

_MAX_STEPS = 6  # tool round-trips before we refuse (2 is the expected path)


class AssistantError(RuntimeError):
    """The agent could not produce a record-backed answer."""


class ApplicationNotFound(AssistantError):
    """The application id does not exist in the LOS."""


def _score_application(app_id: int) -> dict:
    """Score tool: decision-service decisions the app and persists the Reg B record
    atomically (fail closed there). Returns the identifier-free result the model may
    see: enums and numbers only."""
    payload = decision_request_payload(app_id)
    if payload is None:
        raise ApplicationNotFound(f"application {app_id} not found")
    resp = clients.post(clients.DECISION_URL, "/decisions", payload)
    return {
        "status": "recorded",
        "outcome": resp.get("outcome"),
        "score": resp.get("score"),
        "policy_band": resp.get("policy_band"),
        "reason_codes": [r["code"] for r in resp.get("principal_reasons") or []],
    }


def _get_decision_record(app_id: int) -> dict:
    """Memory tool: identifier-free projection of the persisted decision record."""
    resp = clients.get(clients.DECISION_URL, f"/decisions/{app_id}/record")
    if resp.status_code == 404:
        return {"status": "not_found"}
    resp.raise_for_status()
    body = resp.json()
    return {
        "status": body.get("status"),
        "outcome": body.get("outcome"),
        "policy_band": body.get("policy_band"),
        "score": (body.get("drivers") or {}).get("model_score"),
        "reason_codes": [r["code"] for r in body.get("principal_reasons") or []],
    }


_TOOLS = {
    "score_application": _score_application,
    "get_decision_record": _get_decision_record,
}


def _validated_final(action: dict, app_id: int) -> dict:
    """Check the model's final answer against the persisted record (ADR 0009 §5:
    validated, not trusted). Returns the officer-facing result; on mismatch the
    recorded facts win and the narration is replaced."""
    record_resp = clients.get(clients.DECISION_URL, f"/decisions/{app_id}/record")
    if record_resp.status_code == 404:
        raise AssistantError(
            "assistant returned a final answer but no decision record exists — "
            "refusing an unrecorded decision"
        )
    record_resp.raise_for_status()
    record = record_resp.json()
    if record.get("status") != "recorded":
        raise AssistantError(
            "assistant returned a final answer but the application has no recorded "
            "decision event (legacy outcome only) — refusing an unrecorded decision"
        )

    recorded_reasons = record.get("principal_reasons") or []
    recorded_codes = [r["code"] for r in recorded_reasons]
    claimed_outcome = action.get("outcome")
    claimed_codes = action.get("reason_codes") or []
    valid = claimed_outcome == record.get("outcome") and set(claimed_codes) == set(
        recorded_codes
    )
    if valid:
        summary = action.get("summary") or ""
    else:
        log.warning(
            "assistant narration contradicted the record for app_id=%s "
            "(claimed %s/%s, recorded %s/%s) — returning recorded facts",
            app_id,
            claimed_outcome,
            claimed_codes,
            record.get("outcome"),
            recorded_codes,
        )
        reason_text = (
            "; ".join(f"{r['code']}: {r['reason']}" for r in recorded_reasons)
            or "no adverse-action reasons (approval)"
        )
        summary = (
            f"Recorded decision: {record.get('outcome')} "
            f"(policy band {record.get('policy_band')}). {reason_text}."
        )
    return {
        "application_id": app_id,
        "outcome": record.get("outcome"),
        "policy_band": record.get("policy_band"),
        "principal_reasons": recorded_reasons,
        "decided_by": record.get("decided_by"),
        "decided_at": record.get("decided_at"),
        "summary": summary,
        "narration_validated": valid,
    }


def run(application_id: int, client) -> dict:
    """Decision an application through the agent and return the officer-facing result.

    `client` is a ClaudeClient (injected so tests pass a FakeAdapter-backed one).
    Raises AssistantError when the agent cannot produce a record-backed answer, and
    propagates typed LLM errors (budget/transport/validation) and tool HTTP errors.
    """
    history = []
    request = {"application_id": application_id, "task": "decision"}
    for _ in range(_MAX_STEPS):
        action = client.complete(
            "decision_assistant",
            history=history,
            request_json=json.dumps(request),
        )
        kind = action.get("action")
        if kind == "tool":
            name = action.get("tool") or ""
            tool = _TOOLS.get(name)
            if tool is None:
                raise AssistantError(f"assistant requested unknown tool {name!r}")
            # The model's only accepted input is the application id — and we use the
            # ID FROM THE OFFICER'S REQUEST, not the model's echo, so the agent can
            # never wander to another applicant's file.
            result = tool(application_id)
            history.append({"role": "assistant", "content": json.dumps(action)})
            history.append(
                {
                    "role": "user",
                    "content": json.dumps({"tool": name, "result": result}),
                }
            )
            continue
        if kind == "final":
            return _validated_final(action, application_id)
        raise AssistantError(f"assistant returned unknown action {kind!r}")
    raise AssistantError(f"assistant gave no final answer within {_MAX_STEPS} steps")
