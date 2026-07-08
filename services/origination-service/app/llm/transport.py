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

from .adapter import Completion, CompletionRequest, ModelAdapter
from .errors import LLMHTTPError, LLMTimeoutError

_BACKOFF_BASE = 2  # seconds: 2**attempt -> 1, 2, 4, ...


def _backoff_delay(attempt: int, rng: Callable[[], float]) -> float:
    """Equal-jitter exponential backoff for retry `attempt` (0-indexed)."""
    ceiling = _BACKOFF_BASE ** attempt
    return ceiling / 2 + rng() * (ceiling / 2)


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
