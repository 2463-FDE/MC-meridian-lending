"""D28 — the bureau-pull timeout must stay below origination's outer HTTP budget.

`origination-service` `app/clients.py` bounds every KYC/decision/disclosure call at
`_TIMEOUT = 30.0` seconds. Before this fix, `_pull_credit`'s own `httpx.get` to the
bureau used the SAME number (`timeout=30`), so a stalled bureau call could never
surface as THIS service's own bounded refusal -- origination's outer timeout always
expired at the same instant or first, making a stall on this hop indistinguishable
from this service simply not answering at all (origination-service
app/routers/applications.py's `run_decision` had no line naming which one happened).

This test does not import origination-service (separate package, separate
`requirements.txt`, no shared dependency between the two service directories) --
it pins the number origination's `_TIMEOUT` is documented as here and in
docs/debt-log.md D28, so a future change to either side without the other shows up
as a failure here rather than as a silent re-collision.
"""

import httpx
import pytest

from app import config
from app import decision as decision_mod

# Origination-service app/clients.py _TIMEOUT. Mirrored, not imported (see module
# docstring) -- D28's own debt-log entry pins this number for the same reason.
_ORIGINATION_OUTER_TIMEOUT = 30.0


@pytest.fixture
def real_bureau_key(monkeypatch):
    """A configured bureau key, so `_pull_credit` takes the live `httpx.get` path
    instead of the synthetic-score stub the rest of this service's tests use."""
    monkeypatch.setattr(config, "EXPERIAN_KEY", "sk-test-key")
    monkeypatch.setattr(config, "ENVIRONMENT", "production")
    monkeypatch.setattr(config, "ALLOW_SYNTHETIC_CREDIT", False)


def test_bureau_pull_timeout_is_below_originations_outer_budget(
    real_bureau_key, monkeypatch
):
    captured = {}

    def _fake_get(url, params=None, headers=None, timeout=None):
        captured["timeout"] = timeout
        request = httpx.Request("GET", url)
        return httpx.Response(200, request=request, json={"score": 700})

    monkeypatch.setattr(decision_mod.httpx, "get", _fake_get)

    decision_mod._pull_credit("123456782")

    assert captured["timeout"] is not None
    assert captured["timeout"] < _ORIGINATION_OUTER_TIMEOUT, (
        f"bureau-pull timeout ({captured['timeout']}s) is not below origination's "
        f"outer {_ORIGINATION_OUTER_TIMEOUT}s budget for this hop -- a stalled "
        "bureau call can no longer be told apart from origination's own timeout "
        "expiring first (D28)"
    )
