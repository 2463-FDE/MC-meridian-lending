"""Origination service (LOS) — FastAPI.

Endpoints: application intake, KYC (CIP), decisioning, offer/disclosure, and the
LOS->LSS boarding seam. Read paths (list/detail) use SQLAlchemy; the older write paths
(intake, decisioning, boarding) still use raw psycopg2 — a partial, unfinished migration.
"""

import json
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from langsmith.run_helpers import trace
from pydantic import BaseModel

from . import (
    assistant,
    assistant_runs,
    authz,
    clients,
    config,
    disclosure_coordinator,
    intake,
    kyc_gate,
    policy_retrieval,
)
from .llm import ClaudeClient, load_llm_config
from .llm.config import harden_trace_client
from .llm.errors import LLMError
from .logging_config import get_logger
from .routers import applications, offers

log = get_logger("origination")


def _llm_enabled() -> bool:
    """LLM summaries are opt-in. Off by default so a deploy or CI run that does not
    use the feature needs no CLAUDE_API_KEY; on only when explicitly enabled."""
    return os.getenv("LLM_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Claim the LangSmith singleton before anything can trace through it. The agent loop
    # runs inside langgraph, and a framework tracer exports the graph state it is handed
    # — which carries the model's prose and the model-authored policy query. Done
    # unconditionally, and before the client exists: the cost is one object when tracing
    # is off, and the failure it prevents is a span that cannot be un-posted.
    harden_trace_client()
    # Validate the LLM config at boot when the feature is enabled, so a deploy that
    # is missing CLAUDE_API_KEY (provider=anthropic) or carries an invalid CLAUDE_*
    # value fails loud at startup — not silently on the first customer summary.
    # load_llm_config() raises LLMConfigError; letting it propagate aborts startup
    # (uvicorn exits non-zero). Disabled by default, so import/health smoke and any
    # deployment not using summaries start with no LLM env required.
    if _llm_enabled():
        config = load_llm_config()
        app.state.llm_config = config
        app.state.llm_client = ClaudeClient(config)
        log.info("LLM feature enabled; client initialized: %s", config.redacted())
    else:
        app.state.llm_config = None
        app.state.llm_client = None
        log.info("LLM feature disabled (LLM_ENABLED not set); skipping client init")
    yield


app = FastAPI(
    title="Meridian Origination Service (LOS)", version="2.0.0", lifespan=lifespan
)
app.include_router(applications.router)
app.include_router(offers.router)


def get_llm_client(request: Request) -> ClaudeClient:
    """FastAPI dependency for routes that summarize via the LLM. Returns 503 when
    the feature is disabled so a summary route degrades cleanly, not with a 500."""
    client = getattr(request.app.state, "llm_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="LLM feature is not enabled")
    return client


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.error("unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "internal error"})


@app.get("/health")
def health():
    missing = config.missing_required_secrets()
    if missing:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "service": "origination",
                "missing_secrets": missing,
            },
        )
    ok, db_error = config.database_reachable()
    if not ok:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "service": "origination",
                "database_error": db_error,
            },
        )
    return {"status": "ok", "service": "origination"}


@app.post("/assistant/decisions/{app_id}")
def assistant_decide(
    app_id: int,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    client: ClaudeClient = Depends(get_llm_client),
):
    """Decision an application through the officer assistant (ADR 0009 §5).

    The agent's score tool performs the regulated decision + record write in
    decision-service; the response below is validated against that persisted record
    (recorded facts win over narration). Gated by LLM_ENABLED like all LLM routes.

    Optional Idempotency-Key header (same contract as /applications/{app_id}/decision):
    a retry with the same key replays the recorded decision instead of re-pulling credit
    and appending a second regulated event. Absent = explicit re-decision.
    """
    authz.require_officer(x_user_role)
    # Client ask (2026-08-12 governance §5). The client scoped the block to "the one route
    # that runs a decision", having seen only POST /applications/{id}/decision -- but the
    # score tool below performs the same regulated decision and appends the same
    # decision_events record, so gating only that route would leave the finding open
    # through this panel. The read-only GET (explain) never scores and stays open.
    authz.deny_self_decision(app_id, x_user_role, x_user_id)
    if idempotency_key is not None and len(idempotency_key) > 64:
        raise HTTPException(
            status_code=400, detail="Idempotency-Key must be at most 64 characters"
        )
    return _run_assistant(app_id, client, "decision", idempotency_key or None)


@app.get("/assistant/decisions/{app_id}")
def assistant_explain(
    app_id: int,
    policy_topic: str | None = Query(default=None),
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    client: ClaudeClient = Depends(get_llm_client),
):
    """Explain an EXISTING decision from the persisted record (ADR 0009 §5 amendment).

    Read-only: never scores, so asking about an application cannot trigger a fresh
    credit pull. Legacy outcomes (pre-record, e.g. #6012) are answered honestly as
    unrecoverable, distinct from 404 never-decisioned.

    `policy_topic` is optional and is the officer's only channel into policy retrieval
    (ADR 0019). A CODE from a closed vocabulary, never a question: the redaction boundary
    masks free text, so a typed question reaches the model as a placeholder and changes
    nothing. Absent means no policy lookup, which is the read this route has always
    served -- adding the parameter takes nothing away from a caller that omits it.

    An unlisted code is refused HERE with the vocabulary in the message, rather than
    passed down to be masked at the boundary: a masked topic produces a run that looks
    like a policy question nobody could match, which is indistinguishable from a genuine
    abstention. The officer is told their topic does not exist instead.
    """
    authz.require_officer(x_user_role)
    if policy_topic is not None and policy_topic not in policy_retrieval.POLICY_TOPICS:
        raise HTTPException(
            status_code=422,
            detail=(
                "unknown policy_topic; choose one of: "
                + ", ".join(policy_retrieval.POLICY_TOPICS)
            ),
        )
    return _run_assistant(app_id, client, "explain", policy_topic=policy_topic)


@app.get("/applications/{app_id}/summary")
def summarize_application(
    app_id: int,
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    client: ClaudeClient = Depends(get_llm_client),
):
    """Officer triage summary of an application via the loan-summary LLM prompt.

    Officer-only, not officer-or-owner: `recommended_next_step` carries internal triage
    language (`decline_review` / `manual_underwrite`) a borrower must never see about their
    own file. GET because nothing is recorded — sibling `GET /assistant/decisions/{app_id}`
    is precedent for an LLM read. Declared here, not in the router, because `get_llm_client`
    lives in this module (importing it into the router is circular).

    The payload selects zero identity columns (`summary_payload` never joins `applicants`),
    so no applicant name/ssn/dob/address can reach the model; the prompt's `json_vars`
    redaction stays defense-in-depth. Provider/adapter failure maps to 503 "summary
    unavailable" — no `fallback=`, one success shape, the UI says "unavailable" off the 503.
    """
    authz.require_officer(x_user_role)
    payload = applications.summary_payload(app_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="application not found")
    try:
        return client.summarize_application(json.dumps(payload))
    except LLMError as exc:
        log.error("summary LLM failure for app_id=%s: %s", app_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="summary unavailable") from exc


@app.post("/applications/{app_id}/disclosure")
def generate_disclosure(
    app_id: int,
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    client: ClaudeClient = Depends(get_llm_client),
):
    """Generate the TILA disclosure for an approved application (ADR 0012, spec D4).

    Officer-only, like `transition_disclosure` and unlike the rest of the disclosure
    routes (PR review). This previously took `make_offer`'s officer-OR-owner posture on
    the grounds that it persists a regulated document for the application — but it also
    RETURNS that document, plus the officer narration, in the response. A borrower holding
    a valid continuation token could therefore generate and read a draft TILA disclosure
    while its status was still `draft`, which is precisely the state the spec D6 compliance
    hold exists to keep from reaching them; the officer's `review_and_send` /
    `hold_for_compliance` verdict was borrower-visible along with it. The hold is only a
    control if the held document is unreadable until an officer releases it.

    The borrower is not shut out of their own disclosure: `read_disclosure` still admits
    officer, owner, and token-holder, and returns the status and provenance chain. What
    they cannot do is mint the document or read its body before an officer has moved it
    past `draft`.

    Loan terms are bound server-side from the stored application — the caller supplies
    nothing but the id.

    A blocked run returns 422 with the typed reason rather than a 500: "the gate refused
    this document" is a result, not a failure, and the officer needs to see which check
    stopped it.
    """
    authz.require_officer(x_user_role)
    kyc_gate.require_kyc_passed(app_id)

    coordinator = disclosure_coordinator.build_coordinator(client)
    try:
        result = coordinator.run(app_id)
    except disclosure_coordinator.ApplicationNotFound:
        raise HTTPException(status_code=404, detail="application not found")
    except disclosure_coordinator.DownstreamRefused as exc:
        # The service answered and said no (a recompute disagreement, a missing offer) —
        # an answer the officer needs to read, not the generic "unavailable" below.
        log.warning(
            "disclosure pipeline refused app_id=%s status=%s detail=%s",
            app_id,
            exc.status_code,
            exc.detail,
        )
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except LLMError as exc:
        log.error(
            "disclosure pipeline LLM failure app_id=%s: %s", app_id, type(exc).__name__
        )
        raise HTTPException(
            status_code=503, detail="disclosure agent unavailable"
        ) from exc
    except httpx.HTTPError as exc:
        log.error("disclosure pipeline downstream failure app_id=%s: %s", app_id, exc)
        raise HTTPException(
            status_code=502, detail="disclosure service unavailable"
        ) from exc

    if result["status"] == "blocked":
        log.warning(
            "disclosure blocked app_id=%s reason=%s detail=%s",
            app_id,
            result["reason"],
            result.get("detail", ""),
        )
        detail = {
            "status": "blocked",
            "reason": result["reason"],
            "attempts": result.get("attempts", 0),
        }
        # A stage-5 provenance block leaves a persisted draft behind (see the coordinator);
        # hand back its id so the officer can act on the row rather than hunt for it.
        persisted = result.get("disclosure") or {}
        if persisted.get("disclosure_id"):
            detail["disclosure_id"] = persisted["disclosure_id"]
            detail["missing_edges"] = (result.get("provenance") or {}).get(
                "missing_edges", []
            )
        raise HTTPException(status_code=422, detail=detail)
    return result


class TransitionIn(BaseModel):
    to_status: str
    reason_code: str | None = None


@app.get("/applications/{app_id}/disclosure")
def read_disclosure(
    app_id: int,
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_application_token: str | None = Header(default=None, alias="X-Application-Token"),
):
    """The disclosure's status and its provenance chain, read from the KG view.

    Same read posture as `get_offer`: the chain carries the disclosed APR, so officer,
    owner, or token-holder only (ADR 0010).
    """
    authz.require_officer_or_owner(app_id, x_user_role, x_user_id, x_application_token)
    chain = _read_chain(app_id)
    # `policy_band` is the internal underwriting band. Every other route that exposes it
    # (`/assistant/*`) is officer-only; this one admits the owning borrower, so proxying
    # the view verbatim would make an internal risk attribute borrower-visible for the
    # first time. The disclosed figures and the chain itself are theirs to see; the band
    # they were scored into is not, and nothing on this screen uses it.
    chain.pop("policy_band", None)
    return chain


@app.get("/applications/{app_id}/disclosure/document")
def read_disclosure_document(
    app_id: int,
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_application_token: str | None = Header(default=None, alias="X-Application-Token"),
):
    """The stored borrower-facing document (spec D6).

    Officer, owner, or token-holder — but a NON-officer may read only a DELIVERED document.
    A draft body stays officer-only: releasing it to the borrower is exactly what the
    compliance hold exists to prevent, the same reason `generate_disclosure` is officer-only.
    Once delivered, the borrower must be able to read the immutable document they are being
    asked to accept — the `delivered` flag that the boarding guard trusts has to be backed by
    an artifact the borrower can actually see, not merely a status the borrower is told about
    (house rule: every consumer of that flag re-checks the artifact, and the borrower is the
    consumer who acts on delivery by accepting). Delivery is terminal and frozen
    (`trg_disclosures_freeze_delivered`), so the status the chain reports is authoritative for
    this gate.

    Exists because the body used to be readable only in the generating call's response. The
    officer who approves or delivers is a different session — a different person, under
    maker-checker — so without this route the reviewer approved a document they had no way to
    open, and delivery recorded a flag over content nobody had read.

    The disclosure id and status are resolved from the application server-side, same as the
    transition route: that is what binds the read to the application the caller was authorized
    for.
    """
    authz.require_officer_or_owner(app_id, x_user_role, x_user_id, x_application_token)
    chain = _read_chain(app_id)
    disclosure_id = chain.get("disclosure_id")
    if not disclosure_id:
        raise HTTPException(
            status_code=404, detail="no disclosure for this application"
        )
    # Drafts stay officer-only; a borrower/token-holder reads only the DELIVERED body. 404,
    # not 403: an owner asking before delivery gets the same "not available yet" answer the
    # applicant UI already fails closed on, and it never confirms a draft body exists.
    if (
        not authz.is_officer(x_user_role)
        and chain.get("disclosure_status") != "delivered"
    ):
        raise HTTPException(
            status_code=404,
            detail="no delivered disclosure document for this application",
        )
    return _downstream("GET", f"/disclosures/{disclosure_id}/document")


@app.post("/applications/{app_id}/disclosure/transition")
def transition_disclosure(
    app_id: int,
    body: TransitionIn,
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
):
    """Compliance hold and delivery (spec D6): draft -> in_review -> approved -> delivered.

    Officer-only, not officer-OR-owner. Every other disclosure route admits the borrower
    because it is their document; this one is the control that decides whether the
    document is fit to send, and a borrower approving their own TILA disclosure would
    make the compliance hold ceremonial.

    The disclosure id is resolved from the application server-side rather than accepted
    from the caller — that is what binds the transition to the application the caller was
    authorized for.
    """
    authz.require_officer(x_user_role)
    chain = _read_chain(app_id)
    disclosure_id = chain.get("disclosure_id")
    if not disclosure_id:
        raise HTTPException(
            status_code=404, detail="no disclosure for this application"
        )
    return _downstream(
        "POST",
        f"/disclosures/{disclosure_id}/transition",
        json=body.model_dump(),
    )


def _read_chain(app_id: int) -> dict:
    return _downstream("GET", f"/applications/{app_id}/disclosure/provenance")


def _downstream(method: str, path: str, json: dict | None = None) -> dict:
    """Call disclosure-service and preserve its 4xx.

    A 409 "illegal transition" or a TILA timing refusal is an answer the officer needs to
    read, not an outage. Collapsing it into 502 would tell them the service is down when
    what actually happened is that the service said no.
    """
    try:
        if method == "GET":
            resp = clients.get(clients.DISCLOSURE_URL, path)
        else:
            resp = clients.post_raw(clients.DISCLOSURE_URL, path, json)
    except httpx.HTTPError as exc:
        log.error("disclosure-service unreachable path=%s: %s", path, exc)
        raise HTTPException(
            status_code=502, detail="disclosure service unavailable"
        ) from exc
    if 400 <= resp.status_code < 500:
        raise HTTPException(status_code=resp.status_code, detail=_detail(resp))
    if resp.status_code >= 500:
        log.error("disclosure-service %s on %s", resp.status_code, path)
        raise HTTPException(status_code=502, detail="disclosure service unavailable")
    return resp.json()


def _detail(resp: httpx.Response):
    try:
        return resp.json().get("detail", "disclosure request refused")
    except ValueError:
        return "disclosure request refused"


# Entry span for the officer assistant. `assistant.request` (app/assistant.py) is the
# loop root, and it opens AFTER the request is built, the policy topic is checked, the
# KYC gate runs and the application is fetched — so every refusal raised before that
# point produced no trace at all, which is the gap this span closes: one root per
# officer request that reached the assistant, refusal or not.
#
# Two rules hold it to the CONTENT RULE in `app/assistant.py`:
#
#   * metadata is the task, the policy topic (a closed-vocabulary code) and an enum
#     refusal code — never `app_id`, never `request_id`, never a provider message.
#   * NO exception crosses the span boundary. `trace()` attaches `str(exception)` to the
#     span it is raised through, and two of the exceptions caught below carry an
#     application-linked string: `httpx.HTTPStatusError`'s message embeds the request
#     URL (which embeds `app_id`), and an `LLMError` can carry raw provider text. So
#     each one is translated to an enum inside the span and the officer-facing
#     `HTTPException` is raised after the block exits. Re-raising in place would ship to
#     LangSmith exactly what `app/assistant.py` and `app/llm/transport.py` strip.
#
# The classes caught below are every one the assistant path raises: its own
# `ApplicationNotFound`/`AssistantError` (which `run()` also converts
# `GraphRecursionError` into), `LLMError`, the `HTTPException` ADR 0011's KYC gate
# refuses with, and `httpx.HTTPError` — status and transport alike, since a
# `RequestError` is not an `HTTPStatusError` and unreachable is the same outage as 5xx.
#
# Residual, stated rather than implied: an exception outside those classes still crosses
# this span (and is a 500). That is not a new exposure — it already crossed the loop root
# inside `run()` — and the ones that can carry an application-linked string are all caught
# here. `tests/test_trace_error_boundary.py` covers the provider side of the same rule.
_SPAN_ENTRY = "assistant.entry"

# The enums, counts and one bool the entry span promotes from a served result, so a chart
# can group whole requests by outcome. `assistant.request` already records them one level
# down (app/assistant.py, the `root.add_metadata` block), but LangSmith groups by ROOT-run
# metadata, so an outcome-mix chart had to filter child runs while token cost -- which
# rolls up -- sat on the root.
#
# An allowlist, never a spread: `run()` returns the officer-facing summary, the
# `application_id` and the verbatim `policy_citations` text in the same dict, so
# `**result` would ship all three to LangSmith and break both the CONTENT RULE in
# `app/assistant.py` and the no-identifiers rule this span already holds. The two list
# fields are promoted as their LENGTHS for the same reason.
_CHARTED_CODES = ("outcome", "record_status", "policy_band", "narration_validated")
_CHARTED_COUNTS = ("policy_citations", "policy_searches")


def _charted(result: dict) -> dict:
    """Non-content metadata from a served assistant result, for the entry span.

    A field the result does not carry is left OFF the span rather than sent as a null,
    the way `policy_topic` is above: the span says what happened.
    """
    charted = {
        key: result[key]
        for key in _CHARTED_CODES
        if isinstance(result.get(key), (str, bool))
    }
    for key in _CHARTED_COUNTS:
        value = result.get(key)
        if isinstance(value, list):
            charted[key] = len(value)
    return charted


def _run_assistant(
    app_id: int,
    client: ClaudeClient,
    task: str,
    request_id: str | None = None,
    policy_topic: str | None = None,
):
    with trace(
        name=_SPAN_ENTRY,
        run_type="chain",
        metadata={
            "task": task,
            **({"policy_topic": policy_topic} if policy_topic else {}),
        },
    ) as entry:
        # (status, enum refusal code, officer-facing detail, cause) or None on success.
        refusal = None
        started = time.monotonic()
        try:
            result = assistant.run(app_id, client, task, request_id, policy_topic)
        except assistant.ApplicationNeverDecisioned as exc:
            # BEFORE its parent, or the subclass never matches. Same 404 and the same
            # officer-facing detail as `not_found`: what changes is the recorded code,
            # because "the id is not a real application" and "it is real and has not been
            # decisioned yet" are one number today with opposite remedies.
            refusal = (404, "never_decisioned", "application not found", exc)
        except assistant.ApplicationNotFound as exc:
            refusal = (404, "not_found", "application not found", exc)
        except assistant.AssistantError as exc:
            log.error("assistant failed for app_id=%s: %s", app_id, exc)
            refusal = (502, "assistant_refused", str(exc), exc)
        except LLMError as exc:
            log.error(
                "assistant LLM failure for app_id=%s: %s", app_id, type(exc).__name__
            )
            refusal = (503, "llm_unavailable", "assistant unavailable", exc)
        except HTTPException as exc:
            # ADR 0011's KYC gate refuses through the score tool with an `HTTPException`
            # (`app/kyc_gate.py::_block`) rather than one of the assistant's own classes,
            # so its status and detail are already officer-facing and pass through
            # unchanged. Its 409 is a different refusal from the idempotency conflict
            # below and carries its own code, so a trace does not read one as the other.
            code = "kyc_blocked" if exc.status_code == 409 else "refused"
            log.error("assistant refused for app_id=%s: %s", app_id, exc.status_code)
            refusal = (exc.status_code, code, exc.detail, exc)
        except httpx.HTTPError as exc:
            # `HTTPError`, not `HTTPStatusError`: a transport failure reaching
            # decision-service (`httpx.ConnectError`, `httpx.ReadTimeout` — all
            # `httpx.RequestError`, which carries no `response`) is the same outage as
            # that service answering 5xx, and refusing the same way keeps it out of the
            # untranslated 500 path that crosses this span.
            response = getattr(exc, "response", None)
            if response is not None and response.status_code == 409:
                # Reused idempotency key with changed inputs — a conflict, not an outage.
                refusal = (
                    409,
                    "idempotency_conflict",
                    "Idempotency-Key reused with different decision inputs",
                    exc,
                )
            else:
                # The score tool's downstream refusal (e.g. decision-service failing
                # closed on bureau or record write) surfaces as service-unavailable,
                # not a 500.
                log.error("assistant downstream failure for app_id=%s: %s", app_id, exc)
                refusal = (
                    503,
                    "downstream_unavailable",
                    "decisioning unavailable",
                    exc,
                )
        latency_ms = int((time.monotonic() - started) * 1000)
        # `entry.trace_id` rather than `result["trace_id"]`: the same trace either way
        # (`assistant.request` opens inside this span), but it is also populated on the
        # refusal path, where no result exists — and populated when tracing is off, since
        # tracing off means "do not ship spans", not "do not build them".
        trace_id = str(entry.trace_id)
        if refusal is None:
            charted = _charted(result)
            entry.add_metadata({"http_status": 200, **charted})
            assistant_runs.record(
                trace_id=trace_id,
                application_id=app_id,
                task=task,
                policy_topic=policy_topic,
                http_status=200,
                refusal_code=None,
                charted=charted,
                latency_ms=latency_ms,
            )
            return result
        status, code, detail, cause = refusal
        entry.add_metadata({"http_status": status, "refusal": code})
        assistant_runs.record(
            trace_id=trace_id,
            application_id=app_id,
            task=task,
            policy_topic=policy_topic,
            http_status=status,
            refusal_code=code,
            charted={},
            latency_ms=latency_ms,
        )
    # Outside the span on purpose — see the second rule above. The chained `cause` stays
    # for the local traceback, which is inside the client boundary and goes through the
    # redacting formatter; it is the span export that must not see it.
    raise HTTPException(status_code=status, detail=detail) from cause


class BoardIn(BaseModel):
    app_id: int
    applicant_name: str
    principal: float
    annual_rate_pct: float = 7.99
    term_months: int = 48


@app.post("/board")
def board(
    body: BoardIn,
    x_internal_service: str | None = Header(default=None, alias="X-Internal-Service"),
):
    """Legacy direct-boarding endpoint (the LOS->LSS seam). See docs/architecture.md.

    Internal-only (PR review): this creates a loan + balance in servicing from FULLY
    caller-supplied inputs (principal, term, name) with no LOS lookup, and is reachable
    through the gateway's anonymous /los proxy — an external caller could board an
    arbitrary loan. No product caller invokes it (the real flow is /applications/{id}/
    accept); it is retained only as an ops/seam hatch, so it now requires the shared
    internal-service secret, which the gateway strips from external requests.

    ADR 0011 KYC gate NOT applied here (deliberate, parity with the ADR 0010 decision-state
    guard exemption): /board is an internal-only ops hatch with FULLY caller-supplied
    inputs and no LOS lookup, invoked by no product caller. The product boarding path
    (/applications/{id}/accept) IS KYC-gated. Gating this hatch on KYC would presume an LOS
    application it does not read; an operator using it takes explicit responsibility. Noted,
    not missed.

    The delivered-disclosure hold (Reg Z 1026.17(b), PR review) is exempt here for exactly
    the same reason, and this is why that hold is enforced in the accept route rather than
    as a trigger on `loans`: a table-level trigger would also fire for this hatch — whose
    caller-supplied app_id need not name an application with an offer at all — and for the
    demo loans seeded by db/init/002_seed.sql, which carry no disclosures and would fail a
    fresh `make up`. The product path is gated; an operator using this one takes the same
    explicit responsibility as above.
    """
    applications._require_internal_caller(x_internal_service)
    loan_id = intake.board_to_servicing(
        body.app_id,
        body.applicant_name,
        body.principal,
        body.annual_rate_pct,
        body.term_months,
    )
    return {"loan_id": loan_id}
