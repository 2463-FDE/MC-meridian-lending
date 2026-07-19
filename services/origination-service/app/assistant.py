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
import uuid
from urllib.parse import quote

from . import clients, kyc_gate
from .logging_config import get_logger
from .routers.applications import decision_request_payload

log = get_logger("assistant")

_MAX_STEPS = 6  # tool round-trips before we refuse (2 is the expected path)


class AssistantError(RuntimeError):
    """The agent could not produce a record-backed answer."""


class ApplicationNotFound(AssistantError):
    """The application id does not exist in the LOS."""


def _score_application(app_id: int, request_id: str | None = None) -> dict:
    """Score tool: decision-service decisions the app and persists the Reg B record
    atomically (fail closed there). Returns the identifier-free result the model may
    see: enums and numbers only.

    request_id (optional) is forwarded as the decision-service idempotency key so a
    retried officer request replays the recorded decision instead of appending a
    second regulated event (PR #7 review)."""
    payload = decision_request_payload(app_id)
    if payload is None:
        raise ApplicationNotFound(f"application {app_id} not found")
    # ADR 0011 parity: the manual officer route (run_decision) is KYC-gated, so the
    # assistant's score tool must be too -- otherwise "use the assistant" is a KYC bypass
    # for the same regulated credit pull. Fails closed on a declined/absent check.
    kyc_gate.require_kyc_passed(app_id)
    if request_id:
        payload["request_id"] = request_id
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


def _constructed_summary(record: dict) -> str:
    """Deterministic officer summary built purely from the persisted record."""
    if record.get("status") == "no_record_legacy":
        # Honest legacy answer (ADR 0008 req. 4): the outcome exists, the reasons
        # were never captured and cannot be recovered — never invent them.
        return (
            f"Recorded outcome: {record.get('outcome')}. This decision predates the "
            "decision-record system; its reasons were never recorded and cannot be "
            "recovered."
        )
    reasons = record.get("principal_reasons") or []
    reason_text = (
        "; ".join(f"{r['code']}: {r['reason']}" for r in reasons)
        or "no adverse-action reasons (approval)"
    )
    return (
        f"Recorded decision: {record.get('outcome')} "
        f"(policy band {record.get('policy_band')}). {reason_text}."
    )


def _validated_final(
    action: dict, app_id: int, task: str, request_id: str | None = None
) -> dict:
    """Check the model's final answer against the persisted record (ADR 0009 §5:
    validated, not trusted). Returns the officer-facing result; on mismatch the
    recorded facts win and the narration is replaced.

    On the decision task the fetch is scoped to request_id so validation binds to the
    exact event this request created, not the app's latest — a concurrent re-decision
    cannot swap the validated record (PR #7 review). Explain is read-only and
    intentionally reports current app state, so it fetches unscoped."""
    path = f"/decisions/{app_id}/record"
    if request_id and task == "decision":
        path += f"?request_id={quote(request_id, safe='')}"
    record_resp = clients.get(clients.DECISION_URL, path)
    if record_resp.status_code == 404:
        if task == "explain":
            raise ApplicationNotFound(f"application {app_id} was never decisioned")
        raise AssistantError(
            "assistant returned a final answer but no decision record exists — "
            "refusing an unrecorded decision"
        )
    record_resp.raise_for_status()
    record = record_resp.json()
    if record.get("status") != "recorded" and task == "decision":
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
    if not valid:
        log.warning(
            "assistant narration contradicted the record for app_id=%s "
            "(claimed %s/%s, recorded %s/%s) — returning recorded facts",
            app_id,
            claimed_outcome,
            claimed_codes,
            record.get("outcome"),
            recorded_codes,
        )
    # The officer-facing summary is ALWAYS built deterministically from the persisted
    # record — the model's free-form text is never passed through. A matching structured
    # outcome/reason_codes pair does NOT prove the prose is faithful: a model can clear
    # the structured check yet narrate a contradictory or incomplete adverse-action
    # summary (e.g. "approved" text over a recorded deny). Recorded facts win over
    # narration without exception (ADR 0009 §5), so the summary is record-derived and
    # `valid` is retained only as an audit signal on the model's structured claim.
    summary = _constructed_summary(record)
    return {
        "application_id": app_id,
        "record_status": record.get("status"),
        "outcome": record.get("outcome"),
        "policy_band": record.get("policy_band"),
        "principal_reasons": recorded_reasons,
        "decided_by": record.get("decided_by"),
        "decided_at": record.get("decided_at"),
        "summary": summary,
        "narration_validated": valid,
    }


def run(
    application_id: int,
    client,
    task: str = "decision",
    request_id: str | None = None,
) -> dict:
    """Run the agent for one officer request and return the record-backed result.

    task="decision": decision the application (the score tool performs the regulated
    decision + record write). task="explain": read-only — report the existing decision
    from the record; NEVER scores, so asking about an application cannot trigger a
    fresh credit pull.

    request_id (optional): idempotency key forwarded to decision-service — an officer
    request retried with the same key replays the recorded decision rather than
    appending a second regulated event (PR #7 review).

    `client` is a ClaudeClient (injected so tests pass a FakeAdapter-backed one).
    Raises AssistantError when the agent cannot produce a record-backed answer, and
    propagates typed LLM errors (budget/transport/validation) and tool HTTP errors.
    """
    # Always carry a request_id: an officer-supplied key gives cross-request idempotency,
    # and an auto-generated one still binds this run's final validation to the exact
    # event its score tool created (PR #7 review). A fresh key means no replay of a prior
    # event, so an assistant retry without an officer key stays an explicit re-decision.
    request_id = request_id or uuid.uuid4().hex
    history = []
    request = {"application_id": application_id, "task": task}
    score_result = None  # the regulated decision happens AT MOST ONCE per run
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
            if name == "score_application":
                if task == "explain":
                    # Read-only task: a scoring request would be a fresh credit pull
                    # the officer never asked for. Serve the record instead.
                    result = _TOOLS["get_decision_record"](application_id)
                elif score_result is None:
                    score_result = tool(application_id, request_id)
                    result = score_result
                else:
                    # Repeat request returns the cached result — the model cannot
                    # compound bureau pulls or decision events within one request.
                    result = score_result
            else:
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
            return _validated_final(action, application_id, task, request_id)
        raise AssistantError(f"assistant returned unknown action {kind!r}")
    raise AssistantError(f"assistant gave no final answer within {_MAX_STEPS} steps")
