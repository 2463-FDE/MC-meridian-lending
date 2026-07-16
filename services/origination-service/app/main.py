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

from . import assistant, config, intake
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
    if idempotency_key is not None and len(idempotency_key) > 64:
        raise HTTPException(
            status_code=400, detail="Idempotency-Key must be at most 64 characters"
        )
    return _run_assistant(app_id, client, "decision", idempotency_key or None)


@app.get("/assistant/decisions/{app_id}")
def assistant_explain(app_id: int, client: ClaudeClient = Depends(get_llm_client)):
    """Explain an EXISTING decision from the persisted record (ADR 0009 §5 amendment).

    Read-only: never scores, so asking about an application cannot trigger a fresh
    credit pull. Legacy outcomes (pre-record, e.g. #6012) are answered honestly as
    unrecoverable, distinct from 404 never-decisioned.
    """
    return _run_assistant(app_id, client, "explain")


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
def board(body: BoardIn):
    """Legacy direct-boarding endpoint (the LOS->LSS seam). See docs/architecture.md."""
    loan_id = intake.board_to_servicing(
        body.app_id,
        body.applicant_name,
        body.principal,
        body.annual_rate_pct,
        body.term_months,
    )
    return {"loan_id": loan_id}
