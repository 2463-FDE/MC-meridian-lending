"""Per-user soft rate limit on the officer assistant's Bedrock-invoking routes (LLM04).

Unit-level: the counting/window logic in isolation. Route-level wiring (does the route
actually call this and surface a 429) is covered in test_assistant.py.
"""

import pytest
from fastapi import HTTPException

from app import rate_limit

# conftest.py resets rate_limit state before/after every test in the session (its own
# state is shared and global, so this file cannot rely on being run in isolation).


def test_calls_under_the_cap_are_allowed():
    for _ in range(rate_limit._MAX_CALLS):
        rate_limit.check_assistant_rate_limit("officer-1")  # no raise


def test_the_call_past_the_cap_is_refused():
    for _ in range(rate_limit._MAX_CALLS):
        rate_limit.check_assistant_rate_limit("officer-1")

    with pytest.raises(HTTPException) as exc:
        rate_limit.check_assistant_rate_limit("officer-1")
    assert exc.value.status_code == 429


def test_the_cap_is_per_user():
    for _ in range(rate_limit._MAX_CALLS):
        rate_limit.check_assistant_rate_limit("officer-1")

    rate_limit.check_assistant_rate_limit("officer-2")  # separate bucket, no raise


def test_a_missing_user_id_shares_one_bucket_rather_than_going_unmetered():
    for _ in range(rate_limit._MAX_CALLS):
        rate_limit.check_assistant_rate_limit(None)

    with pytest.raises(HTTPException):
        rate_limit.check_assistant_rate_limit(None)


def test_a_call_outside_the_window_does_not_count_against_the_cap(monkeypatch):
    fake_now = [1_000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: fake_now[0])

    for _ in range(rate_limit._MAX_CALLS):
        rate_limit.check_assistant_rate_limit("officer-1")

    fake_now[0] += rate_limit._WINDOW_SECONDS + 1
    rate_limit.check_assistant_rate_limit("officer-1")  # window rolled, no raise
