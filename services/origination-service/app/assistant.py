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
from contextlib import contextmanager
from urllib.parse import quote

from langsmith.run_helpers import trace

from . import clients, kyc_gate, policy_retrieval
from .logging_config import get_logger
from .routers.applications import decision_request_payload

log = get_logger("assistant")

_MAX_STEPS = 6  # tool round-trips before we refuse (2 is the expected path)

# --- root trace ------------------------------------------------------------------
#
# Before this, the only spans in the service were `llm.complete` and its
# `llm.transport` child, so a trace showed that a model call happened and nothing about
# the agent that made it. These hang above them: one root per officer request, with a
# child per loop step, per tool dispatch, per retrieval, and one for the deterministic
# validation that decides what the officer actually reads.
#
# CONTENT RULE, and it is the whole design constraint: a span carries enum codes,
# integers, booleans, and the retrieval's own scores and chunk ids. Never an applicant
# field, never the model's prose, never the model-authored policy query, never corpus
# text. Same posture as `llm.complete`'s input/output strippers
# (`app/llm/client.py`) -- and the reason these are explicit spans rather than a
# framework tracer, which ships whatever state it is handed. That is exactly why
# `disclosure_coordinator.run()` suppresses tracing unconditionally, and nothing here
# relaxes that suppression.
#
# `trace()` is a no-op unless LANGSMITH_TRACING is set, so this costs nothing when the
# feature is off and needs no second code path for the disabled case.
#
# The credit score is deliberately ABSENT from tool spans. It already crosses to the
# provider inside the tool result the model reads, so including it would not be a new
# exposure class -- but it is the most sensitive number in the flow and it tells a trace
# reader nothing that `outcome` and `policy_band` do not. Omitted on least-privilege
# grounds, not because it is unreachable.

_SPAN_REQUEST = "assistant.request"
_SPAN_STEP = "assistant.step"
_SPAN_VALIDATE = "assistant.validate"
_SPAN_RETRIEVAL = "policy.retrieval"

# Enum-valued keys worth lifting from a tool result onto its span. `score` is not here.
_SPAN_RESULT_KEYS = ("status", "outcome", "policy_band")


def _result_metadata(result) -> dict:
    """The enum-only projection of a tool result that may go on a span.

    Type-checked rather than truthiness-checked: a container arriving as something other
    than a dict must not be indexed, and a value arriving as a dict or list must not be
    stringified onto a span.
    """
    if not isinstance(result, dict):
        return {}
    metadata = {}
    for key in _SPAN_RESULT_KEYS:
        value = result.get(key)
        if isinstance(value, str) and value:
            metadata[key] = value
    reasons = result.get("principal_reasons")
    if isinstance(reasons, list):
        # The codes only -- never the reason prose, which is borrower-facing text.
        metadata["reason_codes"] = [
            r.get("code")
            for r in reasons
            if isinstance(r, dict) and isinstance(r.get("code"), str)
        ]
    return metadata


@contextmanager
def _tool_span(name: str):
    """Span for one tool dispatch, named for the tool so the tree reads as the loop ran.

    Yields a recorder rather than the run: a caller holding the run could attach anything
    to it, and the point of these spans is that what they carry is decided in one place.
    """
    with trace(name=f"tool.{name}", run_type="tool") as run:
        yield lambda result: run.add_metadata(_result_metadata(result))


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


def _search_policy(query: str, task: str) -> policy_retrieval.PolicyAnswer:
    """Policy tool (ADR 0019): retrieve one corpus chunk for a model-chosen query.

    Read-only and applicant-free — it takes no application id and touches no applicant
    data. REFUSED on task="decision": the corpus carries the Reg B adverse-action guidance,
    and reason codes are produced deterministically by decision-service, so retrieval must
    stay off the path that produces a regulated outcome (ADR 0019 decision 5).

    The answer's text is officer-facing; only `tool_result()` reaches the model.
    """
    with trace(name=_SPAN_RETRIEVAL, run_type="retriever") as run:
        if task == "decision":
            answer = policy_retrieval.abstain(policy_retrieval.DECISION_TASK)
        else:
            answer = policy_retrieval.search(query)
        # status/reason are closed vocabularies from policy_retrieval, score is a cosine
        # similarity, and chunk_id is a `document#section` id -- corpus metadata, not
        # applicant data. The QUERY and the retrieved TEXT are both absent on purpose:
        # the query is model-authored free text, the text is the passage the officer
        # reads, and a span is not where either of them travels.
        run.add_metadata(
            {
                "status": answer.status,
                "reason": answer.reason,
                "score": round(answer.score, 4),
                "chunk_id": answer.chunk_id if answer.is_hit else None,
                "refused_on_decision_task": task == "decision",
            }
        )
        return answer


_TOOLS = {
    "score_application": _score_application,
    "get_decision_record": _get_decision_record,
    "search_policy": _search_policy,
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


# Officer-facing text for a search that ran but produced nothing to quote.
# Reason-specific (not one generic "no match" line): B1 found that a run
# where retrieval abstained looked identical to a run that never searched at
# all — the false-confidence failure ADR 0019 cites from q11 — and a single
# generic line would still blur "no passage cleared the bar" against "search
# is not configured/available", which are different facts an officer should
# not read as the same thing.
_ABSTAIN_OFFICER_TEXT = {
    policy_retrieval.NO_THRESHOLD: "Policy search is not configured.",
    policy_retrieval.NO_CORPUS: "Policy search found no usable policy corpus.",
    policy_retrieval.EMPTY_QUERY: "Policy search ran but returned no match.",
    policy_retrieval.BELOW_THRESHOLD: (
        "Policy search ran; no passage matched above the required threshold."
    ),
    policy_retrieval.HARNESS_UNAVAILABLE: (
        "Policy search is unavailable in this environment."
    ),
    policy_retrieval.DECISION_TASK: "Policy search is not available on decision runs.",
}


def _no_match_line(searches: list) -> str:
    """Deterministic, code-rendered line for a search that abstained.

    Reports the LAST search attempt's reason — the one closest to the
    officer's actual answer — never the model's narration of it.
    """
    reason = searches[-1].reason
    text = _ABSTAIN_OFFICER_TEXT.get(reason, "Policy search ran; no policy match.")
    return f"Policy search — {text}"


def _policy_section(citations: list) -> str:
    """Officer-facing policy excerpts, quoted VERBATIM from the corpus (ADR 0019).

    Code renders this, not the model: the model never receives the chunk text, so it cannot
    paraphrase or contradict it. Same principle as `_constructed_summary` — the officer reads
    the source of record, not a narration of it. Each excerpt carries its chunk id so the
    officer can open the document the quotation came from.
    """
    blocks = []
    for answer in citations:
        blocks.append(
            f"Policy — {answer.chunk_id} (quoted verbatim from the policy corpus):\n"
            f"{answer.text}"
        )
    return "\n\n".join(blocks)


def _validated_final(
    action: dict,
    app_id: int,
    task: str,
    request_id: str | None = None,
    citations: list | None = None,
    searches: list | None = None,
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
    # Appended to the summary, not returned only as a structured field: the officer screen
    # renders `summary`, so a citation that lived only in `policy_citations` would be
    # retrieved, paid for, and never read by the person who asked for it.
    citations = citations or []
    searches = searches or []
    if citations:
        summary = f"{summary}\n\n{_policy_section(citations)}"
    elif searches:
        # A search ran and came back empty-handed. Without this branch the
        # screen is indistinguishable from a run that never searched at all
        # (B1) — say so, deterministically, from the reason code the module
        # recorded, not from the model's account of what happened.
        summary = f"{summary}\n\n{_no_match_line(searches)}"
    # The model score comes from the persisted record's drivers, so the officer screen can
    # show the SAME decision facts for an assistant run as for a manual Run decision
    # (applications.py run_decision returns score + first principal reason). Without it the
    # primary decision panel goes blank on an assistant-recorded outcome (PR #11 review).
    # Not a model-visible field: this is the officer-facing HTTP response, and the score
    # tool already returns the score to the model. Legacy records carry no model_score, so
    # the key is null rather than absent-by-accident.
    return {
        "application_id": app_id,
        "record_status": record.get("status"),
        "outcome": record.get("outcome"),
        "score": (record.get("drivers") or {}).get("model_score"),
        "policy_band": record.get("policy_band"),
        "principal_reasons": recorded_reasons,
        "decided_by": record.get("decided_by"),
        "decided_at": record.get("decided_at"),
        "summary": summary,
        "narration_validated": valid,
        # Structured twin of the excerpts already inlined above, so a UI can render them
        # as citations rather than parsing the summary text.
        "policy_citations": [
            {
                "chunk_id": answer.chunk_id,
                "score": round(answer.score, 4),
                "text": answer.text,
            }
            for answer in citations
        ],
        # Every search attempt, hit or abstain — never just hits — so the officer
        # screen (and any caller inspecting the response, not just `summary`'s
        # prose) can tell "searched, found nothing" from "never searched" (B1).
        # No chunk_id/text here even for a hit: that identical information is
        # already in policy_citations; this field exists for the abstain case.
        "policy_searches": [
            {
                "status": answer.status,
                "score": round(answer.score, 4),
                "reason": answer.reason,
            }
            for answer in searches
        ],
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
    citations = []  # policy excerpts to quote to the OFFICER (never back to the model)
    searches = []  # every search_policy attempt, hit or abstain (B1: makes an
    # abstention visible instead of indistinguishable from a run that never searched)
    # Root of the trace. `request_id` is the decision idempotency key forwarded to
    # decision-service, so it still ties this run to the exact decision_events row
    # it created or replayed -- but that tie is internal (threaded to the score
    # tool call below), not exported here. `application_id` and `request_id` are
    # caller/applicant-linked identifiers, same exposure class as the idempotency_key
    # `app/llm/client.py` and `app/llm/transport.py` strip before tracing: shipping
    # either to LangSmith would make traces linkable to a specific customer record
    # with no service-owned secret to key an HMAC instead (same omit-vs-hash call as
    # those two spans). Neither is an enum code, integer, boolean, or retrieval score
    # -- the CONTENT RULE above -- so neither belongs on this span at all.
    with trace(
        name=_SPAN_REQUEST,
        run_type="chain",
        metadata={
            "task": task,
            "max_steps": _MAX_STEPS,
        },
    ) as root:
        for step in range(_MAX_STEPS):
            with trace(name=_SPAN_STEP, run_type="chain", metadata={"step": step + 1}):
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
                        raise AssistantError(
                            f"assistant requested unknown tool {name!r}"
                        )
                    with _tool_span(name) as record:
                        # The model's only accepted input is the application id — and we
                        # use the ID FROM THE OFFICER'S REQUEST, not the model's echo, so
                        # the agent can never wander to another applicant's file.
                        if name == "score_application":
                            if task == "explain":
                                # Read-only task: a scoring request would be a fresh
                                # credit pull the officer never asked for. Serve the
                                # record instead.
                                result = _TOOLS["get_decision_record"](application_id)
                            elif score_result is None:
                                score_result = tool(application_id, request_id)
                                result = score_result
                            else:
                                # Repeat request returns the cached result — the model
                                # cannot compound bureau pulls or decision events within
                                # one request.
                                result = score_result
                        elif name == "search_policy":
                            # The one tool whose input is NOT the application id: the
                            # model chooses a query. That query is used here and nowhere
                            # else — it is not echoed into history (see the action
                            # rewrite below) and not logged, because it is model-authored
                            # free text whose contents we do not control.
                            answer = tool(
                                str((action.get("input") or {}).get("query") or ""),
                                task,
                            )
                            searches.append(answer)
                            if answer.is_hit and all(
                                c.chunk_id != answer.chunk_id for c in citations
                            ):
                                citations.append(answer)
                            # Only the allowlisted status + score go back to the model;
                            # the chunk text stays on the officer's side of the boundary
                            # (ADR 0019 decision 3).
                            result = answer.tool_result()
                        else:
                            result = tool(application_id)
                        record(result)
                    if name == "search_policy":
                        # Strip the query before the action is replayed as history. The
                        # redaction contract would mask it anyway
                        # (request_builder._redact_scalar), but a boundary that holds only
                        # because the redactor catches it is one bad allowlist entry from
                        # leaking; drop it at the source instead.
                        action = {k: v for k, v in action.items() if k != "input"}
                    history.append({"role": "assistant", "content": json.dumps(action)})
                    history.append(
                        {
                            "role": "user",
                            "content": json.dumps({"tool": name, "result": result}),
                        }
                    )
                    continue
                if kind == "final":
                    with trace(name=_SPAN_VALIDATE, run_type="chain") as validation:
                        final = _validated_final(
                            action,
                            application_id,
                            task,
                            request_id,
                            citations,
                            searches,
                        )
                        # `narration_validated` is the audit signal on the model's
                        # structured claim; the officer-facing summary is record-derived
                        # either way. Recording it is the point of the span: a trace that
                        # cannot show a narration diverging from the record cannot show
                        # the control working.
                        validation.add_metadata(
                            {
                                "narration_validated": final.get("narration_validated"),
                                "record_status": final.get("record_status"),
                                "outcome": final.get("outcome"),
                                "policy_band": final.get("policy_band"),
                            }
                        )
                    # The business outcome, on the root. Not its own span: a span with no
                    # duration and no children is a metadata bag, and this metadata
                    # describes the request the root already represents.
                    root.add_metadata(
                        {
                            "outcome": final.get("outcome"),
                            "record_status": final.get("record_status"),
                            "policy_band": final.get("policy_band"),
                            "narration_validated": final.get("narration_validated"),
                            "steps_used": step + 1,
                            "policy_citations": len(citations),
                            "policy_searches": len(searches),
                            "scored": score_result is not None,
                        }
                    )
                    return final
                raise AssistantError(f"assistant returned unknown action {kind!r}")
        raise AssistantError(
            f"assistant gave no final answer within {_MAX_STEPS} steps"
        )
