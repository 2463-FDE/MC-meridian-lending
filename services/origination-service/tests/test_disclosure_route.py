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
COMPLETE_CHAIN = {
    "disclosure_id": 5,
    "offer_id": 11,
    "decision_event_id": 7,
    "application_id": 1,
    "applicant_id": 3,
    "chain_complete": True,
    "missing_edges": [],
}


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
    monkeypatch.setattr(
        disclosure_coordinator.LangGraphDisclosureCoordinator,
        "_default_read_provenance",
        staticmethod(lambda disclosure_id: dict(COMPLETE_CHAIN)),
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


def test_a_broken_chain_returns_the_draft_id_so_the_officer_can_act_on_it(
    client, monkeypatch
):
    """A stage-5 provenance block persists the draft before it fails. Returning only
    "blocked" would leave a regulated row the officer has no handle on."""
    _stub_db(monkeypatch)
    monkeypatch.setattr(
        disclosure_coordinator.LangGraphDisclosureCoordinator,
        "_default_read_provenance",
        staticmethod(
            lambda disclosure_id: {
                **COMPLETE_CHAIN,
                "applicant_id": None,
                "chain_complete": False,
                "missing_edges": ["applicant_id"],
            }
        ),
    )
    response = client.post("/applications/1/disclosure", headers=OFFICER)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["reason"] == disclosure_coordinator.BlockReason.PROVENANCE_INCOMPLETE
    assert detail["disclosure_id"] == 5
    assert detail["missing_edges"] == ["applicant_id"]


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


# ---------------------------------------------------------------------------
# Lifecycle proxy (spec D6). The status machine itself lives in disclosure-service; what
# origination owns is who may drive it and whether a refusal reaches the officer intact.
# ---------------------------------------------------------------------------

BORROWER = {"X-User-Role": "borrower", "X-User-Id": "3"}


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def lifecycle(monkeypatch):
    """Stub the two downstream calls and record what was sent."""
    sent = {"gets": [], "posts": []}
    state = {
        "chain": {**COMPLETE_CHAIN, "disclosure_status": "draft"},
        "chain_status": 200,
        "transition": (200, {"disclosure_id": 5, "status": "in_review"}),
    }

    def fake_get(_base, path):
        sent["gets"].append(path)
        return _Resp(state["chain_status"], state["chain"])

    def fake_post(_base, path, payload):
        sent["posts"].append((path, payload))
        return _Resp(*state["transition"])

    monkeypatch.setattr(main.clients, "get", fake_get)
    monkeypatch.setattr(main.clients, "post_raw", fake_post)
    monkeypatch.setattr(main.authz, "require_officer_or_owner", lambda *a, **k: None)
    return sent, state


def test_read_disclosure_returns_the_chain(client, lifecycle):
    sent, _ = lifecycle
    response = client.get("/applications/1/disclosure", headers=OFFICER)

    assert response.status_code == 200
    assert response.json()["disclosure_id"] == 5
    assert sent["gets"] == ["/applications/1/disclosure/provenance"]


def test_transition_is_officer_only_not_officer_or_owner(client, lifecycle):
    """Every other disclosure route admits the borrower because it is their document.
    A borrower approving their own TILA disclosure would make the hold ceremonial."""
    sent, _ = lifecycle
    response = client.post(
        "/applications/1/disclosure/transition",
        json={"to_status": "in_review"},
        headers=BORROWER,
    )

    assert response.status_code == 403
    assert sent["posts"] == [], "must not reach the lifecycle at all"


def test_transition_resolves_the_disclosure_id_server_side(client, lifecycle):
    """The caller names an application, never a disclosure id — that binding is what makes
    the authorization check mean anything."""
    sent, _ = lifecycle
    response = client.post(
        "/applications/1/disclosure/transition",
        json={"to_status": "in_review", "disclosure_id": 999},
        headers=OFFICER,
    )

    assert response.status_code == 200
    assert sent["posts"][0][0] == "/disclosures/5/transition"
    assert "disclosure_id" not in sent["posts"][0][1]


def test_transition_forwards_a_downstream_refusal_verbatim(client, lifecycle):
    """An illegal transition is an answer the officer must read, not an outage."""
    _, state = lifecycle
    state["transition"] = (409, {"detail": "illegal transition delivered -> draft"})
    response = client.post(
        "/applications/1/disclosure/transition",
        json={"to_status": "draft", "reason_code": "wording"},
        headers=OFFICER,
    )

    assert response.status_code == 409
    assert "illegal transition" in response.json()["detail"]


def test_transition_when_there_is_no_disclosure_yet(client, lifecycle):
    _, state = lifecycle
    state["chain"] = {**COMPLETE_CHAIN, "disclosure_id": None}
    response = client.post(
        "/applications/1/disclosure/transition",
        json={"to_status": "in_review"},
        headers=OFFICER,
    )
    assert response.status_code == 404


def test_a_downstream_outage_is_502_not_a_refusal(client, lifecycle, monkeypatch):
    import httpx

    def explode(*_a, **_k):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(main.clients, "get", explode)
    response = client.get("/applications/1/disclosure", headers=OFFICER)
    assert response.status_code == 502


def test_a_downstream_500_is_not_reported_as_a_client_error(client, lifecycle):
    _, state = lifecycle
    state["chain_status"] = 500
    response = client.get("/applications/1/disclosure", headers=OFFICER)
    assert response.status_code == 502


def test_the_internal_policy_band_is_not_proxied_to_the_borrower(client, lifecycle):
    """Every other route exposing `policy_band` is officer-only (`/assistant/*`); this one
    admits the owning borrower. Proxying the view verbatim would make an internal
    underwriting attribute borrower-visible for the first time."""
    _, state = lifecycle
    state["chain"] = {**COMPLETE_CHAIN, "policy_band": "A", "decision_outcome": "approve"}
    body = client.get("/applications/1/disclosure", headers=OFFICER).json()

    assert "policy_band" not in body
    # The outcome stays: the borrower already knows they were approved.
    assert body["decision_outcome"] == "approve"
