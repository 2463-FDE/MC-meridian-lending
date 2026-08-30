"""Payment service — FastAPI.

Standalone card/ACH charge capture extracted from servicing-service. Stores the full PAN
on the payments row (D13b — open: tokenization needs a provider and a card entry surface
this codebase does not have). It no longer stores the CVV: D13a deleted the column and the
values (migration 0020), because PCI-DSS 3.2.1 prohibits retaining sensitive authentication
data after authorization outright. The charge log is redacted at the construction boundary
(D5), and the Idempotency-Key is required and arbitrated by a partial unique index, so a
retried POST no longer double-charges (D19, ADR 0013 Decision 1). The captured amount is
applied to the loan balance by calling servicing-service over HTTP. (D2 — float money —
kept on purpose.)
"""
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config
from .logging_config import get_logger
from .routers import payments

log = get_logger("payment-service")

app = FastAPI(title="Meridian Payment Service", version="2.0.0")
app.include_router(payments.router)


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
            content={"status": "unhealthy", "service": "payment-service", "missing_secrets": missing},
        )
    ok, db_error = config.database_reachable()
    if not ok:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "service": "payment-service", "database_error": db_error},
        )
    return {"status": "ok", "service": "payment-service"}
