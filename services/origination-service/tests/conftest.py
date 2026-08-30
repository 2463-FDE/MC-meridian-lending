"""Shared test config.

hash_token now REFUSES to issue a continuation token outside development without a dedicated
pepper, and /health reports CONTINUATION_TOKEN_KEYS missing in production (PR #7 review). Model
a healthy production config for the whole suite -- a real CONTINUATION_TOKEN_KEYS -- so the
submit/intake and /health paths exercise their actual logic, not the missing-pepper guard.
Tests that specifically exercise the refusal / fallback / rotation monkeypatch it again.
"""

import pytest

from app import config, rate_limit


@pytest.fixture(autouse=True)
def _healthy_continuation_pepper(monkeypatch):
    monkeypatch.setattr(config, "CONTINUATION_TOKEN_KEYS", "test:test-pepper")


@pytest.fixture(autouse=True)
def _reset_assistant_rate_limit():
    # In-process, module-global state (app/rate_limit.py): any test that drives an
    # assistant route through TestClient without a distinct X-User-Id shares the
    # "_no_user_id" bucket with every other such test in the session. Without a reset
    # between tests, whichever test runs 11th trips the real 429 -- a cross-file,
    # run-order-dependent flake, not a bug in the limiter itself.
    rate_limit.reset()
    yield
    rate_limit.reset()
