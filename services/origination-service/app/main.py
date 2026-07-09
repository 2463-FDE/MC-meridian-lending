"""Origination service (LOS) — FastAPI.

Endpoints: application intake, KYC (CIP), decisioning, offer/disclosure, and the
LOS->LSS boarding seam. Read paths (list/detail) use SQLAlchemy; the older write paths
(intake, decisioning, boarding) still use raw psycopg2 — a partial, unfinished migration.
"""
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import config, intake
from .logging_config import get_logger
from .routers import applications, offers

log = get_logger("origination")

app = FastAPI(title="Meridian Origination Service (LOS)", version="2.0.0")
app.include_router(applications.router)
app.include_router(offers.router)


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
            content={"status": "unhealthy", "service": "origination", "missing_secrets": missing},
        )
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
