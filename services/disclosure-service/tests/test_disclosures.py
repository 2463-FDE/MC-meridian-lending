"""Disclosure persistence: the boundary that keeps a regulated number deterministic.

These use a stub session rather than a live Postgres — the DDL's own behaviour (freeze
trigger, unique index, check constraints) was exercised against postgres:16-alpine in the
phase-4 verification and recurs at the `make up` smoke step. What is tested here is the
routing logic that no database can enforce: that the service derives the numbers instead of
accepting them, refuses when its recomputation disagrees with the stored offer, and replays
rather than duplicating.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import fingerprint, models
from app.database import get_session
from app.main import app
from app.routers import disclosures as router_mod
from app.routers import offers as offers_router
from app.schemas import DisclosureIn

TOKEN = "internal-token"

# The worked loan, as offer.build_offer computes it post-phase-2.
GOOD_OFFER = dict(
    apr=9.584,
    finance_charge=3628.71,
    monthly_payment=439.35,
    amount_financed=17460.0,
    total_of_payments=21088.71,
)
GOOD_INPUTS = dict(
    offer_id=1, decision_event_id=7, principal=18000, annual_rate=7.99, term_months=48
)


class StubSession:
    """Enough Session surface for the router: get / scalar / add / commit / refresh."""

    def __init__(self, offer=None, existing=None):
        self.offer = offer
        self.existing = existing
        self.added = []
        self.commits = 0

    def get(self, _model, _pk):
        return self.offer

    def scalar(self, _stmt):
        return self.existing

    def add(self, row):
        row.id = 99
        row.created_at = None
        row.delivered_at = None
        self.added.append(row)

    def commit(self):
        self.commits += 1

    def refresh(self, _row):
        pass

    def rollback(self):
        pass


def _offer(**overrides):
    row = models.Offer(id=1, app_id=42, **{**GOOD_OFFER, **overrides})
    row.decision_event_id = overrides.get("decision_event_id")
    return row


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(offers_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    return TestClient(app)


def _with_session(session):
    app.dependency_overrides[get_session] = lambda: session
    return session


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def test_requires_internal_service_identity(client):
    _with_session(StubSession(offer=_offer()))
    assert client.post("/disclosures", json=GOOD_INPUTS).status_code == 403


def test_unknown_offer_is_not_found(client):
    _with_session(StubSession(offer=None))
    response = client.post(
        "/disclosures", json=GOOD_INPUTS, headers={"X-Internal-Service": TOKEN}
    )
    assert response.status_code == 404


def test_persists_minor_units_derived_from_inputs(client):
    session = _with_session(StubSession(offer=_offer()))
    response = client.post(
        "/disclosures", json=GOOD_INPUTS, headers={"X-Internal-Service": TOKEN}
    )
    assert response.status_code == 201, response.text
    body = response.json()

    # Minor units, and an APR carried as a string — no float at the boundary.
    assert body["finance_charge_cents"] == 362871
    assert body["amount_financed_cents"] == 1746000
    assert body["monthly_payment_cents"] == 43935
    assert body["total_of_payments_cents"] == 2108871
    assert body["apr"] == "9.584"
    assert isinstance(body["apr"], str)
    assert body["status"] == "draft"
    assert body["apr_method_version"] == router_mod.APR_METHOD_VERSION
    assert body["content_fingerprint"].startswith("fp-1:")

    persisted = session.added[0]
    assert persisted.compute_snapshot == {
        "principal_cents": 1800000,
        "note_rate_pct": "7.99",
        "term_months": 48,
        "fee_pct": "0.03",
    }


def test_the_caller_cannot_supply_a_regulated_number():
    """ADR 0012's invariant, enforced by the request schema rather than by convention.

    If DisclosureIn ever grew an `apr` or `finance_charge` field, an upstream agent could
    hand this service a number and it would be persisted as the authoritative record.
    """
    forbidden = {
        "apr",
        "finance_charge",
        "amount_financed",
        "monthly_payment",
        "total_of_payments",
        "content_fingerprint",
    }
    assert forbidden.isdisjoint(DisclosureIn.model_fields)


def test_refuses_when_recomputation_disagrees_with_the_stored_offer(client):
    """A different term reproduces different numbers; persisting anyway would mint a
    disclosure for a loan the borrower was never offered."""
    session = _with_session(StubSession(offer=_offer()))
    response = client.post(
        "/disclosures",
        json={**GOOD_INPUTS, "term_months": 36},
        headers={"X-Internal-Service": TOKEN},
    )
    assert response.status_code == 409
    assert "disagrees with the persisted offer" in response.json()["detail"]
    assert session.added == [], "must not persist a disclosure it just refused"


def test_refuses_when_the_offer_has_no_stored_numbers(client):
    """A NULL money column cannot be shown to agree — fail closed, not open."""
    _with_session(StubSession(offer=_offer(apr=None)))
    response = client.post(
        "/disclosures", json=GOOD_INPUTS, headers={"X-Internal-Service": TOKEN}
    )
    assert response.status_code == 409


def test_replay_returns_the_persisted_record_without_recomputing(client):
    """A retry must return the document that exists, not a freshly derived one — a rules
    change between attempts must not swap the borrower's disclosure."""
    existing = models.Disclosure(
        id=5,
        offer_id=1,
        decision_event_id=7,
        status="approved",
        apr=Decimal("5.041"),
        finance_charge_cents=1,
        amount_financed_cents=1,
        monthly_payment_cents=1,
        total_of_payments_cents=1,
        compute_snapshot={},
        fee_schedule_version="old",
        apr_method_version="add-on-legacy",
        content_fingerprint="fp-1:old",
    )
    existing.delivered_at = None
    session = _with_session(StubSession(offer=_offer(), existing=existing))
    response = client.post(
        "/disclosures", json=GOOD_INPUTS, headers={"X-Internal-Service": TOKEN}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["disclosure_id"] == 5
    assert body["apr"] == "5.041"
    assert body["apr_method_version"] == "add-on-legacy"
    assert session.added == []


def test_closes_the_provenance_edge_on_a_legacy_offer(client):
    session = _with_session(StubSession(offer=_offer(decision_event_id=None)))
    client.post("/disclosures", json=GOOD_INPUTS, headers={"X-Internal-Service": TOKEN})
    assert session.offer.decision_event_id == 7


class TestFingerprint:
    def test_is_stable_across_calls(self):
        args = dict(
            inputs={"principal_cents": 1800000, "term_months": 48},
            fee_schedule_version="2024-11",
            apr_method_version="actuarial-regz-appj-1",
            outputs={"apr": Decimal("9.584"), "finance_charge_cents": 362871},
        )
        assert fingerprint.compute_fingerprint(
            **args
        ) == fingerprint.compute_fingerprint(**args)

    def test_changes_when_any_component_changes(self):
        base = dict(
            inputs={"principal_cents": 1800000},
            fee_schedule_version="2024-11",
            apr_method_version="actuarial-regz-appj-1",
            outputs={"apr": Decimal("9.584")},
        )
        digests = {
            fingerprint.compute_fingerprint(**base),
            fingerprint.compute_fingerprint(
                **{**base, "inputs": {"principal_cents": 1800001}}
            ),
            fingerprint.compute_fingerprint(
                **{**base, "fee_schedule_version": "2025-01"}
            ),
            fingerprint.compute_fingerprint(**{**base, "apr_method_version": "add-on"}),
            fingerprint.compute_fingerprint(
                **{**base, "outputs": {"apr": Decimal("9.585")}}
            ),
        }
        assert len(digests) == 5

    def test_rejects_float_input(self):
        """A float would make the digest depend on platform formatting — for a record
        meant to reproduce years later, that is worse than no fingerprint."""
        with pytest.raises(TypeError, match="float in fingerprint payload"):
            fingerprint.compute_fingerprint(
                inputs={"principal": 18000.0},
                fee_schedule_version="v",
                apr_method_version="v",
                outputs={},
            )
