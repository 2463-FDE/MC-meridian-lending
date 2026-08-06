"""Disclosure service — FastAPI.

Extracts the TILA / Reg-Z offer + APR + amortization disclosure logic out of the LOS into
a standalone service. Read paths (latest offer lookup) use SQLAlchemy; the offer write path
still uses raw psycopg2 + float money — the partial-migration seam carried over verbatim.
"""

import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config, rules
from .logging_config import get_logger
from .routers import disclosures, offers

log = get_logger("disclosure")

app = FastAPI(title="Meridian Disclosure Service", version="2.0.0")
app.include_router(offers.router)
app.include_router(disclosures.router)


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
                "service": "disclosure-service",
                "missing_secrets": missing,
            },
        )
    # Fail closed on policy config: without a loadable fee schedule this service cannot
    # justify the fee inside a regulated disclosure, so it must not look ready.
    rules_error = rules.config_error()
    if rules_error:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "service": "disclosure-service",
                "rules_config_error": rules_error,
            },
        )
    ok, db_error = config.database_reachable()
    if not ok:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "service": "disclosure-service",
                "database_error": db_error,
            },
        )
    return {"status": "ok", "service": "disclosure-service"}
