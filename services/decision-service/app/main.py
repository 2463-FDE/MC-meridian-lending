"""Decision service — FastAPI.

Standalone credit-decisioning service, extracted from the origination service (LOS).
Exposes the synchronous decisioning chain (bureau pull + rules scorecard) and persists
the bare outcome to the shared `decisions` table. The decisioning write path uses raw
psycopg2 — the same partial, unfinished ORM migration seam as origination.
"""
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config
from .logging_config import get_logger
from .routers import decisions

log = get_logger("decision-service")

app = FastAPI(title="Meridian Decision Service", version="2.0.0")
app.include_router(decisions.router)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.error("unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "internal error"})


@app.get("/health")
def health():
    # Fail readiness when a required secret is missing, so a keyless deployment
    # cannot look healthy while issuing decisions off a synthetic score.
    missing = config.missing_required_secrets()
    if missing:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "service": "decision-service",
                "missing_secrets": missing,
            },
        )
    return {"status": "ok", "service": "decision-service"}
