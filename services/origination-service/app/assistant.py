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

import httpx
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent
from langsmith.run_helpers import trace
from pydantic import BaseModel, Field

from . import clients, kyc_gate, policy_retrieval
from .llm.chat_model import MeridianChatModel
from .logging_config import get_logger
from .routers.applications import decision_request_payload

log = get_logger("assistant")

_MAX_STEPS = 6  # tool round-trips before we refuse (2 is the expected path)

# What `_MAX_STEPS` becomes once the framework owns the loop. langgraph counts NODE
# executions, not model calls, and one round-trip is two nodes (the model, then the
# tool): measured at recursion_limit=6, the graph made 3 provider calls before
# stopping. So 2x is the exact translation — `_MAX_STEPS` model calls and no more.
# NOT `2x + 1`: that buys one extra model call past the budget, which is a paid call
# the refusal was supposed to prevent.
#
# Exhaustion has TWO shapes on langgraph 1.2.10 and both were measured here, which is
# why neither one alone is trusted:
#
#   * the SOFT stop -- `create_react_agent` tracks `remaining_steps` and, when the
#     model node is the one that runs out, appends
#     `AIMessage("Sorry, need more steps to process this request.")` and returns
#     NORMALLY. A caller reading the return value would hand framework prose to an
#     officer as an answer.
#   * the HARD stop -- when the tool node hits the wall instead, langgraph raises
#     `GraphRecursionError`.
#
# Which one fires depends on where the node count lands, so `run()` handles both: it
# catches `GraphRecursionError` and it checks the terminal message. Do not simplify
# this to one branch by picking a limit whose parity favours one shape -- that is
# fitting to an implementation detail of the framework's step accounting.
_RECURSION_LIMIT = 2 * _MAX_STEPS

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

    `marks` are extra keys the CALL SITE knows and the result does not — whether a score
    request was served from the cache, whether it was substituted on the explain path.
    Booleans and enum codes only, same CONTENT RULE as everything else on these spans.
    """
    with trace(name=f"tool.{name}", run_type="tool") as run:
        yield lambda result, **marks: run.add_metadata(
            {**_result_metadata(result), **marks}
        )


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
        # Identifier-free on purpose: this raises through the tool/step/root trace
        # spans, and langsmith's trace() attaches str(exception) as that span's
        # `error` field, so an app_id in the message would ship to LangSmith
        # through the error channel even after B1 closed the metadata channel.
        # No caller reads this message either -- main.py maps the TYPE to a fixed
        # 404 detail.
        raise ApplicationNotFound("application not found")
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
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # httpx's own message embeds the request URL (`/decisions/{app_id}/record`),
        # so re-raise with the app_id stripped and `from None` -- otherwise the
        # traceback trace() ships to LangSmith on error still carries the chained
        # original exception's URL.
        raise AssistantError(
            f"decision-service returned {exc.response.status_code} reading the "
            "decision record"
        ) from None
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


# --- the framework's tool set ------------------------------------------------------
#
# `create_agent` binds tools ONCE, so the three invariants the hand-rolled loop applied
# at dispatch time have to live inside the closures instead. That is a strengthening,
# not a port: these closures take no application id at all, so the officer's id is not
# "preferred over" the model's -- the model has no way to name one.


class _NoArgs(BaseModel):
    """No arguments.

    The application is the officer's, taken from their own request, so there is nothing
    for the model to supply. Pydantic ignores unknown fields by default, which is what
    makes this the pinning: a tool call carrying `{"application_id": 99}` validates to
    `{}` and the closure runs against the officer's application regardless.
    """


class _PolicyQuery(BaseModel):
    """`search_policy`'s only argument, and the only model-authored input in the system.

    Optional rather than required: an omitted query would otherwise raise inside the
    framework's tool executor, which returns the error to the model as prose -- and
    prose has no block shape the redaction boundary carries, so a recoverable model
    mistake would become a refused turn. `policy_retrieval.search` already abstains on
    an empty query, which is the honest answer to one.
    """

    query: str = Field(
        default="", description="A question about written lending policy."
    )


def _framework_tools(application_id: int, request_id: str, task: str):
    """The three tools, closed over this request, plus the state the answer needs.

    Returns `(tools, state)`. `state` collects what the officer-facing answer is built
    from -- the citations, every search attempt, and whether the regulated decision
    ran -- because the framework owns the message list and none of that can be read
    back out of it.
    """
    state: dict = {"score": None, "citations": [], "searches": []}

    def _call(name: str, *args):
        """Run a tool from `_TOOLS`, resolved at CALL time.

        Resolved late on purpose: a swap that snapshots the table at import time would
        silently ignore a test's monkeypatch, and the whole tool surface is tested that
        way.
        """
        tool = _TOOLS.get(name)
        if tool is None:  # pragma: no cover - the table is module-owned
            raise AssistantError(f"assistant requested unknown tool {name!r}")
        return tool(*args)

    def _score() -> str:
        # One span per REQUEST, named for the tool the model asked for — not per
        # dispatch. A model that asks to score six times and is served the cache five
        # times is a trace worth reading, and a span only where work happened would
        # show one call and hide the interlock doing its job.
        #
        # Interlock 2 (explain never scores): a score request on a read-only task is a
        # billable credit pull the officer did not ask for, so it is served from the
        # record instead. Interlock 1 (one regulated decision per run): the second
        # request in a run gets the first result, so the model cannot compound bureau
        # pulls or decision_events.
        with _tool_span("score_application") as record:
            if task == "explain":
                result = _call("get_decision_record", application_id)
                record(result, substituted_on_explain=True)
                return json.dumps(result)
            cached = state["score"] is not None
            if not cached:
                state["score"] = _call("score_application", application_id, request_id)
            record(state["score"], served_from_cache=cached)
            return json.dumps(state["score"])

    def _record() -> str:
        with _tool_span("get_decision_record") as record:
            result = _call("get_decision_record", application_id)
            record(result)
            return json.dumps(result)

    def _policy(query: str = "") -> str:
        # Interlock 5 (PT-001): search_policy is honoured AT MOST ONCE per run, the same
        # cap as the regulated score, so the model cannot compound retrieval calls or
        # citations within one run. A repeat request is served the first answer -- and
        # gets its own span, marked, for the same reason a repeat score does: the trace
        # should show the model asking twice and the cap holding.
        with _tool_span("search_policy") as record:
            if state["searches"]:
                answer = state["searches"][-1]
                record(answer.tool_result(), served_from_cache=True)
                return json.dumps(answer.tool_result())
            answer = _call("search_policy", query, task)
            record(answer.tool_result(), served_from_cache=False)
        state["searches"].append(answer)
        if answer.is_hit and all(
            c.chunk_id != answer.chunk_id for c in state["citations"]
        ):
            state["citations"].append(answer)
        # Only the allowlisted status + score go back to the model; the chunk text
        # stays on the officer's side of the boundary (ADR 0019 decision 3).
        return json.dumps(answer.tool_result())

    tools = [
        StructuredTool.from_function(
            func=_score,
            name="score_application",
            description=(
                "Score the application under review and persist its decision record. "
                "Takes no arguments: the application is the one the officer asked "
                "about."
            ),
            args_schema=_NoArgs,
        ),
        StructuredTool.from_function(
            func=_record,
            name="get_decision_record",
            description=(
                "Read the persisted decision record for the application under review. "
                "Takes no arguments."
            ),
            args_schema=_NoArgs,
        ),
        StructuredTool.from_function(
            func=_policy,
            name="search_policy",
            description=(
                "Look up one passage of Meridian's written lending policy. Read-only, "
                "and refused while producing a decision."
            ),
            args_schema=_PolicyQuery,
        ),
    ]
    return tools, state


def _build_agent(client, tools):
    """The framework agent that owns the loop.

    Kept as a named module-level function so a test can assert what the request path
    actually runs on, rather than inferring it from an import.

    `create_react_agent` calls `bind_tools` on the model, which is where the schemas
    become a real provider `tools` field, and it is reached from the existing
    `langgraph` pin -- `langchain.agents.create_agent` would require moving
    `langchain-core` and `langgraph` under the disclosure pipeline's own StateGraph.
    """
    return create_react_agent(MeridianChatModel(client=client), tools=tools)


def _terminal_action(messages: list) -> dict:
    """The final action the graph ended on, or a refusal.

    This is interlock 4's soft half. langgraph's recursion limit does not always
    raise: when the model node is the one that runs out, `create_react_agent` appends
    its own `AIMessage("Sorry, need more steps to process this request.")` and returns,
    so a run that never answered comes back looking like a run that did. The hard half
    -- `GraphRecursionError` -- is caught in `run()`.

    The framework's sentence is never quoted into the error. It is not an officer-facing
    answer and `main.py` maps the TYPE, not the message.
    """
    last = messages[-1] if messages else None
    content = getattr(last, "content", None)
    if isinstance(content, str) and content.strip():
        try:
            action = json.loads(content)
        except json.JSONDecodeError:
            action = None
        if isinstance(action, dict) and action.get("action") == "final":
            return action
    raise AssistantError(f"assistant gave no final answer within {_MAX_STEPS} steps")


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
            # Identifier-free (B1 follow-up): this raises through the root trace
            # span, and a raw app_id here would ship to LangSmith via trace()'s
            # `error` field even though the metadata dict never carried it.
            raise ApplicationNotFound("application was never decisioned")
        raise AssistantError(
            "assistant returned a final answer but no decision record exists — "
            "refusing an unrecorded decision"
        )
    try:
        record_resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # `path` embeds app_id and request_id; httpx's own error message embeds
        # `path`. Re-raise scrubbed, `from None` so the chained original (with the
        # raw URL) doesn't reappear in the traceback trace() ships on error.
        raise AssistantError(
            f"decision-service returned {exc.response.status_code} validating the "
            "decision record"
        ) from None
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
    policy_topic: str | None = None,
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
    request = {"application_id": application_id, "task": task}
    # The officer's policy topic, and the only thing in this request the officer chooses.
    # Without it the model has no reason to call search_policy at all: the request said
    # nothing but "explain application N", and a typed question cannot be plumbed here
    # because the boundary masks free text (`_SAFE_CATEGORICAL`) -- an officer's sentence
    # would arrive as a redaction placeholder. So the channel is a CODE.
    #
    # Re-validated here rather than trusted from the route: this function has a second
    # caller in the tests, and an unlisted code would reach the model as that same
    # placeholder instead of being refused.
    if policy_topic is not None:
        if policy_topic not in policy_retrieval.POLICY_TOPICS:
            raise AssistantError(f"unknown policy topic {policy_topic!r}")
        request["policy_topic"] = policy_topic
    # The per-run caps that were loop-local before the swap now live in the tool
    # closures, which is where a framework can still see them: `create_agent` binds
    # tools once, so a check in the loop body would have nowhere to run.
    tools, state = _framework_tools(application_id, request_id, task)
    agent = _build_agent(client, tools)
    # Root of the trace. `request_id` is the decision idempotency key forwarded to
    # decision-service, so it still ties this run to the exact decision_events row
    # it created or replayed -- but that tie is internal (threaded into the score
    # closure above), not exported here. `application_id` and `request_id` are
    # caller/applicant-linked identifiers, same exposure class as the idempotency_key
    # `app/llm/client.py` and `app/llm/transport.py` strip before tracing: shipping
    # either to LangSmith would make traces linkable to a specific customer record
    # with no service-owned secret to key an HMAC instead (same omit-vs-hash call as
    # those two spans). Neither is an enum code, integer, boolean, or retrieval score
    # -- the CONTENT RULE above -- so neither belongs on this span at all.
    #
    # There is no `assistant.step` span any more, and that is the swap: the framework
    # owns the loop, so its own node runs ARE the steps, and a hand-emitted span beside
    # them would be a second name for one thing. Everything below the root that this
    # service owns is unchanged -- `llm.complete`/`llm.transport` per model call,
    # `tool.*` per dispatch, `policy.retrieval` under the policy tool, and
    # `assistant.validate` over the record check.
    with trace(
        name=_SPAN_REQUEST,
        run_type="chain",
        metadata={
            "task": task,
            "max_steps": _MAX_STEPS,
            # An enum code from a closed vocabulary, so it satisfies the CONTENT RULE
            # above on the same footing as `task`: it records what the officer asked
            # about without carrying anything they typed, because there is nothing to
            # type. Absent from the span when the officer asked no policy question,
            # rather than present as a null -- the span says what happened.
            **({"policy_topic": policy_topic} if policy_topic else {}),
        },
    ) as root:
        try:
            result = agent.invoke(
                {"messages": [HumanMessage(content=json.dumps(request))]},
                config={"recursion_limit": _RECURSION_LIMIT},
            )
        except GraphRecursionError as exc:
            # The hard half of interlock 4. Translated, not propagated: `main.py` maps
            # AssistantError to the officer-facing refusal, and an unmapped framework
            # exception is a 500 where a refusal was intended. The framework's message
            # is not quoted -- it names its own config key, which is not an answer.
            raise AssistantError(
                f"assistant gave no final answer within {_MAX_STEPS} steps"
            ) from exc
        messages = result.get("messages") or []
        action = _terminal_action(messages)
        if policy_topic is not None and task == "explain" and not state["searches"]:
            # The officer asked a policy question (policy_topic set) — a final answer
            # that never called search_policy would reach the officer with an empty
            # policy_searches/no citation, indistinguishable from a run that genuinely
            # searched and found nothing (PT-001). Enforce in code, not the prompt:
            # refuse the final outright. Checked against `state["searches"]` rather
            # than a loop-local flag, because the framework owns the loop now — an
            # abstention still counts as a search, which is why the list is the test
            # and not the citations.
            raise AssistantError(
                "assistant returned a final answer without calling "
                "search_policy for the requested policy_topic — refusing an "
                "unsearched policy answer"
            )
        with trace(name=_SPAN_VALIDATE, run_type="chain") as validation:
            final = _validated_final(
                action,
                application_id,
                task,
                request_id,
                state["citations"],
                state["searches"],
            )
            # `narration_validated` is the audit signal on the model's structured
            # claim; the officer-facing summary is record-derived either way.
            # Recording it is the point of the span: a trace that cannot show a
            # narration diverging from the record cannot show the control working.
            validation.add_metadata(
                {
                    "narration_validated": final.get("narration_validated"),
                    "record_status": final.get("record_status"),
                    "outcome": final.get("outcome"),
                    "policy_band": final.get("policy_band"),
                }
            )
        # The business outcome, on the root. Not its own span: a span with no duration
        # and no children is a metadata bag, and this metadata describes the request
        # the root already represents. `steps_used` is counted from the message list
        # the framework returned, since the loop is no longer ours to count.
        root.add_metadata(
            {
                "outcome": final.get("outcome"),
                "record_status": final.get("record_status"),
                "policy_band": final.get("policy_band"),
                "narration_validated": final.get("narration_validated"),
                "steps_used": sum(
                    1 for m in messages if getattr(m, "type", "") == "ai"
                ),
                "policy_citations": len(state["citations"]),
                "policy_searches": len(state["searches"]),
                "scored": state["score"] is not None,
            }
        )
        return final
