"""The disclosure endpoint and its production gather.

The coordinator tests run the graph with everything injected. What is left to prove is the
wiring those tests bypass: that the route enforces the same authorization and KYC posture
as `make_offer`, that loan terms are bound from the stored application rather than the
caller, and that a blocked run surfaces as a typed 422 instead of a 500.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app import disclosure_coordinator, main
from app.llm import ClaudeClient, FakeAdapter
from app.llm.config import LLMConfig

FIGURES = {
    "apr": "9.584",
    "finance_charge": "3628.71",
    "amount_financed": "17460.00",
    "total_of_payments": "21088.71",
    "monthly_payment": "439.35",
}
OFFICER = {"X-User-Role": "underwriter", "X-User-Id": "1"}


def _document(figures=None) -> str:
    return json.dumps(
        {
            "heading": "Truth in Lending Disclosure",
            "figures": figures or FIGURES,
            "payment_terms": "48 monthly payments.",
            "prepayment": "No penalty.",
        }
    )


def _narration() -> str:
    return json.dumps({"summary": "Ready.", "officer_action": "review_and_send"})


@pytest.fixture
def client(monkeypatch):
    """Wire the app with a FakeAdapter-backed LLM client and no live downstreams."""
    config = LLMConfig(api_key="test-key", model="claude-test", max_tokens=512)
    llm = ClaudeClient(
        config, adapter=FakeAdapter(responses=[_document(), _narration()])
    )
    monkeypatch.setattr(main.app.state, "llm_client", llm, raising=False)

    monkeypatch.setattr(main.authz, "require_officer_or_owner", lambda *a, **k: None)
    monkeypatch.setattr(main.kyc_gate, "require_kyc_passed", lambda *a, **k: None)
    monkeypatch.setattr(
        disclosure_coordinator.LangGraphDisclosureCoordinator,
        "_default_compute_offer",
        staticmethod(lambda payload: {"offer_id": 11, "disclosure": dict(FIGURES)}),
    )
    monkeypatch.setattr(
        disclosure_coordinator.LangGraphDisclosureCoordinator,
        "_default_persist",
        staticmethod(lambda payload: {"disclosure_id": 5, "status": "draft"}),
    )
    return TestClient(main.app)


def _stub_db(monkeypatch, *, application=True, event=("approve", 7)):
    """Stand in for the two queries gather_disclosure_context makes."""
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        if "FROM applications" in sql:
            return [{"amount": 18000.0, "term_months": 48}] if application else []
        if "FROM decision_events" in sql:
            if event is None:
                return []
            outcome, event_id = event
            return [{"id": event_id, "outcome": outcome}]
        return []

    monkeypatch.setattr(disclosure_coordinator.db, "query", query)
    return calls


def test_generates_a_disclosure_for_an_approved_application(client, monkeypatch):
    _stub_db(monkeypatch)
    response = client.post("/applications/1/disclosure", headers=OFFICER)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["document"]["figures"] == FIGURES
    assert body["disclosure"]["disclosure_id"] == 5


def test_unknown_application_is_404_not_500(client, monkeypatch):
    _stub_db(monkeypatch, application=False)
    assert (
        client.post("/applications/999/disclosure", headers=OFFICER).status_code == 404
    )


def test_a_blocked_run_returns_the_typed_reason(client, monkeypatch):
    """ "The gate refused this" is a result the officer must see, not an outage."""
    _stub_db(monkeypatch, event=("deny", 7))
    response = client.post("/applications/1/disclosure", headers=OFFICER)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["reason"] == disclosure_coordinator.BlockReason.NOT_APPROVED


def test_an_application_with_no_decision_event_cannot_produce_a_disclosure(
    client, monkeypatch
):
    _stub_db(monkeypatch, event=None)
    response = client.post("/applications/1/disclosure", headers=OFFICER)

    assert response.status_code == 422
    assert (
        response.json()["detail"]["reason"]
        == disclosure_coordinator.BlockReason.PROVENANCE_INCOMPLETE
    )


def test_requires_authorization(client, monkeypatch):
    """ADR 0010: /los/* is reachable anonymously through the gateway, and this route
    persists a regulated document — it must not authorize on the caller's say-so."""
    _stub_db(monkeypatch)

    def deny(*_a, **_k):
        raise main.HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(main.authz, "require_officer_or_owner", deny)
    assert client.post("/applications/1/disclosure").status_code == 403


def test_requires_passing_kyc(client, monkeypatch):
    """ADR 0011, defense in depth alongside the decision gate."""
    _stub_db(monkeypatch)

    def deny(*_a, **_k):
        raise main.HTTPException(status_code=409, detail="kyc required")

    monkeypatch.setattr(main.kyc_gate, "require_kyc_passed", deny)
    assert client.post("/applications/1/disclosure", headers=OFFICER).status_code == 409


class TestGatherBindsServerSide:
    def test_terms_come_from_the_stored_application_and_policy(self, monkeypatch):
        """The caller supplies an id and nothing else. Accepting caller terms here would
        make an anonymously-reachable route able to mint a disclosure for fabricated
        numbers — the confused deputy make_offer already closed."""
        _stub_db(monkeypatch)
        context = disclosure_coordinator.gather_disclosure_context(1)

        assert context["principal"] == 18000.0
        assert context["term_months"] == 48
        from app.routers.offers import POLICY_RATE_PCT

        assert context["annual_rate"] == POLICY_RATE_PCT

    def test_outcome_is_read_from_the_append_only_event_not_the_mutable_pointer(
        self, monkeypatch
    ):
        """`decisions` is a current-state pointer; `decision_events` is the system of
        record (ADR 0009) and the row the provenance edge points at."""
        calls = _stub_db(monkeypatch)
        disclosure_coordinator.gather_disclosure_context(1)

        queried = " ".join(sql for sql, _ in calls)
        assert "FROM decision_events" in queried
        assert "FROM decisions " not in queried

    def test_the_latest_event_wins(self, monkeypatch):
        """A re-decision supersedes: the disclosure must cite the decision in force."""
        calls = _stub_db(monkeypatch)
        disclosure_coordinator.gather_disclosure_context(1)

        event_sql = next(sql for sql, _ in calls if "FROM decision_events" in sql)
        assert "ORDER BY decided_at DESC" in event_sql
        assert "LIMIT 1" in event_sql

    def test_missing_application_raises_not_found(self, monkeypatch):
        _stub_db(monkeypatch, application=False)
        with pytest.raises(disclosure_coordinator.ApplicationNotFound):
            disclosure_coordinator.gather_disclosure_context(999)
