"""Origination service (LOS) — FastAPI.

Endpoints: application intake, KYC (CIP), decisioning, offer/disclosure, and the
LOS->LSS boarding seam. Read paths (list/detail) use SQLAlchemy; the older write paths
(intake, decisioning, boarding) still use raw psycopg2 — a partial, unfinished migration.
"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import intake
from .llm import ClaudeClient, load_llm_config
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
    return {"status": "ok", "service": "origination"}


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
        body.app_id, body.applicant_name, body.principal,
        body.annual_rate_pct, body.term_months,
    )
    return {"loan_id": loan_id}
