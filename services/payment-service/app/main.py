"""Payment service — FastAPI.

Standalone card/ACH charge capture extracted from servicing-service. Stores the full PAN
and CVV on the payments row, logs the full charge request (PAN/CVV/SSN) at INFO, and has
NO idempotency key — a retried POST double-charges. The captured amount is applied to the
loan balance by calling servicing-service over HTTP. (D2, D5, D13 — kept on purpose)
"""
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .logging_config import get_logger
from .routers import payments

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = get_logger("payment-service")

app = FastAPI(title="Meridian Payment Service", version="2.0.0")
app.include_router(payments.router)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.error("unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "internal error"})


@app.get("/health")
def health():
    return {"status": "ok", "service": "payment-service"}
