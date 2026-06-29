"""Decisioning tests for the rules scorecard (these PASS).

Both tests use the deterministic stub bureau path: there is no live Experian in the test
environment, so `_pull_credit` falls back to its deterministic stub (680 for an SSN ending
in an even digit, 612 otherwise). Persistence is best-effort and swallowed when no DB is
present, so these tests exercise the scorecard outcome without a database.

NOTE (intentional debt, left UNTESTED): there is deliberately NO test asserting that a
decision audit trail / reason-code accuracy exists. The `decisions` table stores OUTCOME
ONLY (no reason, no model drivers, no inputs, no timestamp), and the adverse-action reason
is a generic nearest-checkbox string — that debt (D4, D10, twists #1/#2) stays untested.
"""
from app.decision import decide


def test_clear_approve():
    # SSN ends in an even digit -> stub bureau score 680; high income clears the scorecard.
    result = decide({"app_id": 1, "ssn": "123456782", "income": 100000})
    assert result["decision"] == "approve"
    assert result["score"] >= 660


def test_clear_deny():
    # SSN ends in an odd digit -> stub bureau score 612; zero income sinks the scorecard.
    result = decide({"app_id": 2, "ssn": "123456781", "income": 0})
    assert result["decision"] == "deny"
    assert result["score"] < 600
