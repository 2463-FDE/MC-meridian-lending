"""Concern 7 — Logger wiring.

The LLM client reuses the service's existing redacting logger (`get_logger` in
`app/logging_config.py`, which routes every line through `PiiRedactor`). We do
not build a second logging stack — we borrow the hardened one so LLM log lines
get the same PII redaction as the rest of the service for free.

The client logs metrics only (latency, token counts, model, retries, request id)
and never the API key or raw request/response content; the redactor is the
belt-and-suspenders backstop.
"""
from ..logging_config import get_logger


def get_llm_logger():
    """Return the redacting logger used for all LLM client output."""
    return get_logger("llm")
