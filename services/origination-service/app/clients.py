"""HTTP clients for the extracted KYC / decision / disclosure microservices.

Origination (LOS) used to run CIP, decisioning, and offer/disclosure in-process. Those
were extracted into standalone services; this module is the thin httpx seam that replaces
the old direct function calls. Base URLs come from config (env-driven) with the docker
network http://<svc>:<port> defaults.
"""
import httpx

from .config import DECISION_URL, DISCLOSURE_URL, KYC_URL  # noqa: F401  (re-exported)
from .logging_config import get_logger

log = get_logger("clients")

_TIMEOUT = 30.0


def post(base_url: str, path: str, payload: dict) -> dict:
    """POST JSON to a downstream service, raise on non-2xx, return the decoded body."""
    resp = httpx.post(f"{base_url}{path}", json=payload, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get(base_url: str, path: str) -> httpx.Response:
    """GET a downstream service; return the raw response so callers can branch on status
    (e.g. forward a 404 instead of treating it as a 500)."""
    return httpx.get(f"{base_url}{path}", timeout=_TIMEOUT)
