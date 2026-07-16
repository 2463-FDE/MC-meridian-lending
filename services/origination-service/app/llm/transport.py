"""Concern 4 — Resilient transport.

Wraps a `ModelAdapter.complete` call with bounded retries. Policy:

- **Timeout** is enforced on every call by the adapter (the timeout is on the
  `CompletionRequest`); a timeout surfaces as `LLMTimeoutError`.
- **Retry only transient failures**: HTTP 429 and 5xx (`LLMHTTPError.retryable`).
  4xx failures — bad request, auth, budget — raise immediately with no retry.
  Timeouts are *not* retried here (checklist scopes retry to 429/5xx); revisit if
  operational data shows timeouts are usefully retryable.
- **Exponential backoff + jitter** between attempts (base 2: ~1s, 2s, 4s), with
  equal jitter to avoid a thundering herd when the API recovers.
- **Idempotency**: LLM completion has no server side effect, so replaying the
  same request is safe. The `idempotency_key` is threaded through and logged as
  the request id, making retries traceable.

`sleep` and `rng` are injectable so tests drive the retry path deterministically
without real delays.
"""

from __future__ import annotations

import random
import time
from typing import Callable

from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree

from .adapter import Completion, CompletionRequest, ModelAdapter
from .errors import LLMHTTPError, LLMTimeoutError

_BACKOFF_BASE = 2  # seconds: 2**attempt -> 1, 2, 4, ...

# LangSmith prices canonical model names, not Bedrock inference-profile ids
# (e.g. "us.anthropic.claude-haiku-4-5-20251001-v1:0" carries no price). Map the
# provider model string to a name LangSmith's cost table recognizes; unknown
# strings pass through unchanged (still traced, just uncosted).
_CANONICAL_MODEL = {
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": "claude-haiku-4-5",
    "claude-haiku-4-5-20251001": "claude-haiku-4-5",
}


def _canonical_model(model: str) -> str:
    return _CANONICAL_MODEL.get(model, model)


def _trace_transport_inputs(inputs: dict) -> dict:
    """Export only NON-CONTENT request metadata to LangSmith.

    `build_request` redacts identity PII but deliberately keeps the business
    facts the model needs (loan amount, income, employment tenure, purpose,
    history). Serializing `system`/`messages` would therefore ship customer
    lending content to a separate telemetry vendor — a privacy/compliance
    exposure, not just token/cost metadata (PR review). So the prompt body is
    NEVER traced by default: only model, sizing/sampling params, timeout, and
    retry budget — none of which carry application content. Drops the adapter
    object and injected callables too.

    idempotency_key is NOT traced (review finding): complete() accepts it verbatim
    from callers, so an upstream caller keying on an application number, customer
    reference, or email-derived value would ship that identifier to a third-party
    telemetry vendor and make traces linkable to customer records. Omitted here
    (and on the parent llm.complete span) rather than hashed, because no service
    -owned secret exists to key an HMAC and an unkeyed hash of a low-entropy id is
    reversible. LangSmith's own run ids provide trace correlation; if request-id
    correlation is ever needed, export a keyed HMAC with a provisioned secret.
    """
    req = inputs.get("req")
    if req is None:
        return {}
    return {
        "model": req.model,
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
        "timeout": req.timeout,
        "max_retries": inputs.get("max_retries"),
    }


def _trace_transport_outputs(completion: Completion) -> dict:
    """Shape the traced output so LangSmith computes token cost.

    NEVER exports `completion.text`: at this layer the model output is raw and
    unvalidated — `validate_structured`/`guard_output` run in the client AFTER
    `call_with_retry` returns, so tracing the text here would ship output the
    client may reject (echoed/injected PII, overlong or malformed responses) to
    the third-party sink. The parent `llm.complete` span likewise traces only
    non-content metadata about its result, not the validated body (PR review);
    this span carries only token usage, stop reason, and model metadata.

    `usage_metadata` is the shape LangSmith's cost engine reads; without it the
    span shows latency but no tokens and no cost. Values come from the
    Completion the adapter already fills.
    """
    return {
        "stop_reason": completion.stop_reason,
        "usage_metadata": {
            "input_tokens": completion.input_tokens,
            "output_tokens": completion.output_tokens,
            "total_tokens": completion.input_tokens + completion.output_tokens,
        },
    }


def _backoff_delay(attempt: int, rng: Callable[[], float]) -> float:
    """Equal-jitter exponential backoff for retry `attempt` (0-indexed)."""
    ceiling = _BACKOFF_BASE**attempt
    return ceiling / 2 + rng() * (ceiling / 2)


@traceable(
    name="llm.transport",
    run_type="llm",
    process_inputs=_trace_transport_inputs,
    process_outputs=_trace_transport_outputs,
)
def call_with_retry(
    adapter: ModelAdapter,
    req: CompletionRequest,
    *,
    max_retries: int,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] = random.random,
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> Completion:
    """Call `adapter.complete(req)`, retrying transient failures.

    Total attempts = 1 + `max_retries`. Raises the last error if retries are
    exhausted, or immediately on a non-retryable error.
    """
    # Tag the LLM run with a priceable model name so LangSmith can cost it.
    # req.model is known up front; canonicalize the Bedrock profile id.
    run_tree = get_current_run_tree()
    if run_tree is not None:
        run_tree.metadata["ls_model_name"] = _canonical_model(req.model)

    attempt = 0
    while True:
        try:
            return adapter.complete(req)
        except LLMHTTPError as exc:
            if not exc.retryable or attempt >= max_retries:
                raise
            # Python unbinds `exc` when the except block exits, so it can't be
            # referenced below. Keep it under a plain name across the block.
            last_exc = exc
        except LLMTimeoutError:
            # Not retried by policy (see module docstring).
            raise

        delay = _backoff_delay(attempt, rng)
        if on_retry is not None:
            on_retry(attempt + 1, delay, last_exc)
        sleep(delay)
        attempt += 1
