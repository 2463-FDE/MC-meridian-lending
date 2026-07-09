"""KYC service — FastAPI.

Runs Customer Identification Program (CIP) verification only and persists a kyc_checks
row. The CIP logic was extracted from origination-service; the gap is carried forward —
no OFAC/sanctions screening, no beneficial-owner (UBO) capture, no ongoing monitoring, no
SAR path. (debt D11)
"""
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config
from .logging_config import get_logger
from .routers import kyc

log = get_logger("kyc")

app = FastAPI(title="Meridian KYC Service", version="2.0.0")
app.include_router(kyc.router)


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
            content={"status": "unhealthy", "service": "kyc-service", "missing_secrets": missing},
        )
    return {"status": "ok", "service": "kyc-service"}
