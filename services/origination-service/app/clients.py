"""HTTP clients for the extracted KYC / decision / disclosure microservices.

Origination (LOS) used to run CIP, decisioning, and offer/disclosure in-process. Those
were extracted into standalone services; this module is the thin httpx seam that replaces
the old direct function calls. Base URLs come from config (env-driven) with the docker
network http://<svc>:<port> defaults.
"""

import httpx

from .config import (  # noqa: F401  (re-exported)
    DECISION_URL,
    DISCLOSURE_URL,
    INTERNAL_SERVICE_TOKEN,
    KYC_URL,
)
from .logging_config import get_logger

log = get_logger("clients")

_TIMEOUT = 30.0


def _internal_headers() -> dict:
    """Identify these calls as internal service-to-service so downstream internal-only
    routes (decision-service record read) accept them. Empty when the token is unset —
    the downstream route then fails closed rather than trusting an empty header."""
    return (
        {"X-Internal-Service": INTERNAL_SERVICE_TOKEN} if INTERNAL_SERVICE_TOKEN else {}
    )


def post(base_url: str, path: str, payload: dict) -> dict:
    """POST JSON to a downstream service, raise on non-2xx, return the decoded body."""
    resp = httpx.post(
        f"{base_url}{path}", json=payload, timeout=_TIMEOUT, headers=_internal_headers()
    )
    resp.raise_for_status()
    return resp.json()


def get(base_url: str, path: str) -> httpx.Response:
    """GET a downstream service; return the raw response so callers can branch on status
    (e.g. forward a 404 instead of treating it as a 500)."""
    return httpx.get(f"{base_url}{path}", timeout=_TIMEOUT, headers=_internal_headers())
