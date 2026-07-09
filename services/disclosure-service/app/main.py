"""Disclosure service — FastAPI.

Extracts the TILA / Reg-Z offer + APR + amortization disclosure logic out of the LOS into
a standalone service. Read paths (latest offer lookup) use SQLAlchemy; the offer write path
still uses raw psycopg2 + float money — the partial-migration seam carried over verbatim.
"""
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config
from .logging_config import get_logger
from .routers import offers

log = get_logger("disclosure")

app = FastAPI(title="Meridian Disclosure Service", version="2.0.0")
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
            content={"status": "unhealthy", "service": "disclosure-service", "missing_secrets": missing},
        )
    return {"status": "ok", "service": "disclosure-service"}
