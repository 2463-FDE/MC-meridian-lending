"""Origination service (LOS) — FastAPI.

Endpoints: application intake, KYC (CIP), decisioning, offer/disclosure, and the
LOS->LSS boarding seam. Read paths (list/detail) use SQLAlchemy; the older write paths
(intake, decisioning, boarding) still use raw psycopg2 — a partial, unfinished migration.
"""

import os
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import (
    assistant,
    authz,
    clients,
    config,
    disclosure_coordinator,
    intake,
    kyc_gate,
)
from .llm import ClaudeClient, load_llm_config
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
    if idempotency_key is not None and len(idempotency_key) > 64:
        raise HTTPException(
            status_code=400, detail="Idempotency-Key must be at most 64 characters"
        )
    return _run_assistant(app_id, client, "decision", idempotency_key or None)


@app.get("/assistant/decisions/{app_id}")
def assistant_explain(
    app_id: int,
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    client: ClaudeClient = Depends(get_llm_client),
):
    """Explain an EXISTING decision from the persisted record (ADR 0009 §5 amendment).

    Read-only: never scores, so asking about an application cannot trigger a fresh
    credit pull. Legacy outcomes (pre-record, e.g. #6012) are answered honestly as
    unrecoverable, distinct from 404 never-decisioned.
    """
    authz.require_officer(x_user_role)
    return _run_assistant(app_id, client, "explain")


@app.post("/applications/{app_id}/disclosure")
def generate_disclosure(
    app_id: int,
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_application_token: str | None = Header(default=None, alias="X-Application-Token"),
    client: ClaudeClient = Depends(get_llm_client),
):
    """Generate the TILA disclosure for an approved application (ADR 0012, spec D4).

    Same authorization posture as `make_offer`, because this persists a regulated document
    for the application: officer, owning borrower, or the applicant holding this
    application's continuation token (ADR 0010), and a passing KYC (ADR 0011). Loan terms
    are bound server-side from the stored application — the caller supplies nothing but
    the id.

    A blocked run returns 422 with the typed reason rather than a 500: "the gate refused
    this document" is a result, not a failure, and the officer needs to see which check
    stopped it.
    """
    authz.require_officer_or_owner(app_id, x_user_role, x_user_id, x_application_token)
    kyc_gate.require_kyc_passed(app_id)

    coordinator = disclosure_coordinator.build_coordinator(client)
    try:
        result = coordinator.run(app_id)
    except disclosure_coordinator.ApplicationNotFound:
        raise HTTPException(status_code=404, detail="application not found")
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
    return _read_chain(app_id)


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


def _run_assistant(
    app_id: int, client: ClaudeClient, task: str, request_id: str | None = None
):
    try:
        return assistant.run(app_id, client, task, request_id)
    except assistant.ApplicationNotFound:
        raise HTTPException(status_code=404, detail="application not found")
    except assistant.AssistantError as exc:
        log.error("assistant failed for app_id=%s: %s", app_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except LLMError as exc:
        log.error("assistant LLM failure for app_id=%s: %s", app_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="assistant unavailable") from exc
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 409:
            # Reused idempotency key with changed inputs — a conflict, not an outage.
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key reused with different decision inputs",
            ) from exc
        # The score tool's downstream refusal (e.g. decision-service failing closed
        # on bureau or record write) surfaces as service-unavailable, not a 500.
        log.error("assistant downstream failure for app_id=%s: %s", app_id, exc)
        raise HTTPException(status_code=503, detail="decisioning unavailable") from exc


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
