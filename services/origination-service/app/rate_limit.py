"""Per-user soft rate limit on Bedrock-invoking routes (LLM04 -- unbounded cost/DoS).

Every route in origination-service that invokes a Bedrock model shares this limiter
(the two officer-assistant routes, the application summary, and the disclosure
generator); nothing else in the service calls Bedrock. This is a speed bump against
a runaway client or script, not a security boundary: state is in-process, a restart
clears it, and a second instance would not share it with the first. Scaling
origination-service past one instance needs a shared store (Redis, already used
elsewhere in this repo) instead -- not built here, since nothing today runs more
than one instance.
"""

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException

_WINDOW_SECONDS = 60
_MAX_CALLS = 10

_lock = threading.Lock()
_calls: dict[str, deque] = defaultdict(deque)


def check_llm_rate_limit(user_id: str | None) -> None:
    """Raise 429 once `user_id` has made `_MAX_CALLS` Bedrock-invoking calls in the
    last `_WINDOW_SECONDS`, across every route that shares this limiter. An absent
    user id shares one bucket rather than going unmetered -- the gateway always
    forwards X-User-Id for an authenticated officer, so this only bites a caller
    that reaches origination-service directly."""
    key = user_id or "_no_user_id"
    now = time.monotonic()
    with _lock:
        window = _calls[key]
        while window and now - window[0] > _WINDOW_SECONDS:
            window.popleft()
        if len(window) >= _MAX_CALLS:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"assistant rate limit exceeded: max {_MAX_CALLS} calls per "
                    f"{_WINDOW_SECONDS}s"
                ),
            )
        window.append(now)


def reset() -> None:
    """Drop all counters (tests only, mirrors policy_retrieval.reset_index_cache)."""
    with _lock:
        _calls.clear()
