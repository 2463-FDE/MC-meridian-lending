"""Shared test config.

hash_token now REFUSES to issue a continuation token outside development without a dedicated
pepper, and /health reports CONTINUATION_TOKEN_KEYS missing in production (PR #7 review). Model
a healthy production config for the whole suite -- a real CONTINUATION_TOKEN_KEYS -- so the
submit/intake and /health paths exercise their actual logic, not the missing-pepper guard.
Tests that specifically exercise the refusal / fallback / rotation monkeypatch it again.
"""

import pytest

from app import config


@pytest.fixture(autouse=True)
def _healthy_continuation_pepper(monkeypatch):
    monkeypatch.setattr(config, "CONTINUATION_TOKEN_KEYS", "test:test-pepper")
