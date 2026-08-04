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
from sqlalchemy.exc import IntegrityError

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


_UNSET = object()


class StubResult:
    def __init__(self, row, rowcount=0):
        self._row = row
        self.rowcount = rowcount

    def mappings(self):
        return self

    def first(self):
        return self._row


class StubSession:
    """Enough Session surface for the router: get / scalar / execute / add / commit."""

    def __init__(
        self,
        offer=None,
        existing=None,
        provenance_row=None,
        disclosure=None,
        consummated=False,
        update_rowcount=1,
        decision_event_app_id=_UNSET,
        winner=None,
        commit_error=None,
    ):
        self.offer = offer
        self.existing = existing
        # The concurrent-insert race: `existing` answers the pre-compute replay lookup,
        # `winner` answers the post-IntegrityError one, so one stub can play both sides.
        self.winner = winner
        self.commit_error = commit_error
        self.scalar_calls = 0
        self.provenance_row = provenance_row
        self.disclosure = disclosure
        self.consummated = consummated
        self.update_rowcount = update_rowcount
        # Default: the decision event belongs to this offer's own application, so tests
        # that don't care about the identity check are unaffected. Pass an explicit
        # app_id (or None, for "no such decision event") to exercise it.
        self.decision_event_app_id = decision_event_app_id
        self.statements = []
        self.added = []
        self.commits = 0

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append((sql, params))
        if sql.strip().upper().startswith("UPDATE"):
            if self.update_rowcount and self.disclosure is not None:
                # Stand in for expire-on-commit: the router reads the row back after the
                # UPDATE, so the stub must reflect what the UPDATE wrote.
                for field, value in statement.compile().params.items():
                    if hasattr(self.disclosure, field):
                        setattr(self.disclosure, field, value)
            return StubResult(None, rowcount=self.update_rowcount)
        if "FROM loans" in sql:
            return StubResult((1,) if self.consummated else None)
        if "FROM decision_events" in sql:
            app_id = (
                self.decision_event_app_id
                if self.decision_event_app_id is not _UNSET
                else (self.offer.app_id if self.offer is not None else None)
            )
            return StubResult(None if app_id is None else (app_id,))
        return StubResult(self.provenance_row)

    def get(self, model, _pk):
        if model is models.Disclosure:
            return self.disclosure
        return self.offer

    def scalar(self, _stmt):
        self.scalar_calls += 1
        return self.existing if self.scalar_calls == 1 else self.winner

    def add(self, row):
        row.id = 99
        row.created_at = None
        row.delivered_at = None
        self.added.append(row)

    def commit(self):
        self.commits += 1
        if self.commit_error is not None and self.commits == 1:
            raise self.commit_error

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


def test_refuses_a_decision_event_from_a_different_application(client):
    """The FK on decision_event_id proves the row exists; it does not prove it belongs to
    THIS offer's application. Without this check, any internal caller supplying a
    valid-but-wrong-applicant decision event mints a disclosure whose provenance view
    would report chain_complete: true — indistinguishable from a correct chain, and worse
    than the partial-chain case the view already handles because it is silent."""
    session = _with_session(StubSession(offer=_offer(), decision_event_app_id=999))
    response = client.post(
        "/disclosures", json=GOOD_INPUTS, headers={"X-Internal-Service": TOKEN}
    )
    assert response.status_code == 409
    assert "does not belong" in response.json()["detail"]
    assert session.added == [], "must not persist a disclosure with a mismatched chain"


def test_refuses_a_decision_event_that_does_not_exist(client):
    session = _with_session(StubSession(offer=_offer(), decision_event_app_id=None))
    response = client.post(
        "/disclosures", json=GOOD_INPUTS, headers={"X-Internal-Service": TOKEN}
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "decision event not found"


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


def _persisted_disclosure(**overrides):
    row = models.Disclosure(
        **{
            "id": 5,
            "offer_id": 1,
            "decision_event_id": 7,
            "status": "draft",
            "apr": Decimal("9.584"),
            "finance_charge_cents": 362871,
            "amount_financed_cents": 1746000,
            "monthly_payment_cents": 43935,
            "total_of_payments_cents": 2108871,
            "compute_snapshot": {},
            "fee_schedule_version": "2024-11",
            "apr_method_version": "actuarial-regz-appj-1",
            "content_fingerprint": "fp-1",
            **overrides,
        }
    )
    row.delivered_at = None
    return row


def test_the_edge_and_the_record_commit_together(client):
    """One transaction, not two. The edge write used to follow the insert's commit, which
    is the window that leaves a persisted disclosure with an open edge."""
    session = _with_session(StubSession(offer=_offer(decision_event_id=None)))
    response = client.post(
        "/disclosures", json=GOOD_INPUTS, headers={"X-Internal-Service": TOKEN}
    )
    assert response.status_code == 201, response.text
    assert session.offer.decision_event_id == 7
    assert session.commits == 1, "the provenance edge must not commit separately"


def test_replay_repairs_an_edge_a_crashed_write_left_open(client):
    """The replay path returns before any compute, so it is also the only path a
    half-written record can ever reach again. Without a repair here the offer edge stays
    NULL forever, `v_disclosure_provenance` keeps reporting the chain incomplete for a
    disclosure that exists, and delivery cannot proceed without hand-written SQL.

    The repair source is the persisted disclosure (decision event 3 below), not the
    request body (7) — the body is not revalidated on this path."""
    session = _with_session(
        StubSession(
            offer=_offer(decision_event_id=None),
            existing=_persisted_disclosure(decision_event_id=3),
        )
    )
    response = client.post(
        "/disclosures", json=GOOD_INPUTS, headers={"X-Internal-Service": TOKEN}
    )
    assert response.status_code == 201, response.text
    assert session.added == [], "a replay must not persist a second record"
    assert session.offer.decision_event_id == 3
    assert session.commits == 1


def test_the_concurrent_loser_repairs_the_edge_before_replaying(client):
    """The loser rolls back, which discards its own edge write too. It has to close the
    edge from the winner's record, or the race reproduces the same open edge the crash
    window does."""
    session = _with_session(
        StubSession(
            offer=_offer(decision_event_id=None),
            winner=_persisted_disclosure(id=11, decision_event_id=7),
            commit_error=IntegrityError(
                "insert", {}, Exception("uq_disclosures_offer")
            ),
        )
    )
    response = client.post(
        "/disclosures", json=GOOD_INPUTS, headers={"X-Internal-Service": TOKEN}
    )
    assert response.status_code == 201, response.text
    assert response.json()["disclosure_id"] == 11
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


# ---------------------------------------------------------------------------
# The KG read (spec D3). What matters here is not just the payload but WHERE it
# comes from: one statement, against the view, with no join reconstructed in code.
# ---------------------------------------------------------------------------

CHAIN_ROW = {
    "disclosure_id": 5,
    "disclosure_status": "draft",
    "disclosed_apr": Decimal("9.584"),
    "compute_snapshot": {
        "principal_cents": 1800000,
        "note_rate_pct": "7.99",
        "term_months": 48,
        "fee_pct": "0.03",
    },
    "fee_schedule_version": "2026.07",
    "apr_method_version": "actuarial-regz-appj-1",
    "content_fingerprint": "fp-1:abc",
    "delivered_at": None,
    "offer_id": 1,
    "offer_apr": 9.584,
    "offer_created_at": None,
    "decision_event_id": 7,
    "decision_outcome": "approve",
    "policy_band": "A",
    "decided_at": None,
    "application_id": 42,
    "applicant_id": 3,
}


def test_provenance_requires_internal_service_identity(client):
    _with_session(StubSession(provenance_row=CHAIN_ROW))
    assert client.get("/disclosures/5/provenance").status_code == 403


def test_provenance_unknown_disclosure_is_not_found(client):
    _with_session(StubSession(provenance_row=None))
    response = client.get(
        "/disclosures/5/provenance", headers={"X-Internal-Service": TOKEN}
    )
    assert response.status_code == 404


def test_provenance_returns_the_whole_chain_in_one_view_query(client):
    """Acceptance D3.3: given a disclosure id, ONE view query returns the full chain
    including the exact inputs and the rule versions used."""
    session = _with_session(StubSession(provenance_row=CHAIN_ROW))
    response = client.get(
        "/disclosures/5/provenance", headers={"X-Internal-Service": TOKEN}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["chain_complete"] is True
    assert body["missing_edges"] == []
    # Every hop of disclosure -> offer -> decision_event -> application -> applicant.
    assert (body["disclosure_id"], body["offer_id"], body["decision_event_id"]) == (
        5,
        1,
        7,
    )
    assert (body["application_id"], body["applicant_id"]) == (42, 3)
    # Inputs and rule versions, so the figures can be recomputed from this one read.
    assert body["compute_snapshot"]["principal_cents"] == 1800000
    assert body["fee_schedule_version"] == "2026.07"
    assert body["apr_method_version"] == "actuarial-regz-appj-1"
    # NUMERIC crosses the boundary as a string, same as DisclosureOut.
    assert body["disclosed_apr"] == "9.584"

    assert len(session.statements) == 1, "the chain must be one query, not a walk"


def test_provenance_reads_the_view_and_does_not_rejoin_the_chain(client):
    """Spec D3 is a code-structure requirement: the traversal is defined once, in the
    view. A hand-rolled join here would be a second definition free to drift from the one
    an auditor reads."""
    session = _with_session(StubSession(provenance_row=CHAIN_ROW))
    client.get("/disclosures/5/provenance", headers={"X-Internal-Service": TOKEN})

    sql = session.statements[0][0]
    assert "FROM v_disclosure_provenance" in sql
    assert "JOIN" not in sql.upper()


def test_provenance_reports_a_partial_chain_rather_than_hiding_it(client):
    """A legacy offer predates `offers.decision_event_id`, so its chain genuinely breaks.
    Reporting it is the point of the view; raising would make the worst rows invisible."""
    _with_session(
        StubSession(
            provenance_row={
                **CHAIN_ROW,
                "decision_event_id": None,
                "decision_outcome": None,
                "applicant_id": None,
            }
        )
    )
    response = client.get(
        "/disclosures/5/provenance", headers={"X-Internal-Service": TOKEN}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["chain_complete"] is False
    assert body["missing_edges"] == ["decision_event_id", "applicant_id"]


# ---------------------------------------------------------------------------
# Lifecycle (spec D6). The freeze trigger and the CHECK constraints live in the DDL and
# were exercised against postgres:16-alpine; what these cover is the machine above them.
# ---------------------------------------------------------------------------


def _disclosure(status="draft", delivered_at=None):
    row = models.Disclosure(
        id=5,
        offer_id=1,
        decision_event_id=7,
        status=status,
        apr=Decimal("9.584"),
        finance_charge_cents=362871,
        amount_financed_cents=1746000,
        monthly_payment_cents=43935,
        total_of_payments_cents=2108871,
        compute_snapshot={},
        fee_schedule_version="2026.07",
        apr_method_version="actuarial-regz-appj-1",
        content_fingerprint="fp-1:abc",
    )
    row.delivered_at = delivered_at
    return row


def _transition(client, body, disclosure_id=5):
    return client.post(
        f"/disclosures/{disclosure_id}/transition",
        json=body,
        headers={"X-Internal-Service": TOKEN},
    )


def test_transition_requires_internal_service_identity(client):
    _with_session(StubSession(disclosure=_disclosure()))
    response = client.post("/disclosures/5/transition", json={"to_status": "in_review"})
    assert response.status_code == 403


def test_transition_unknown_disclosure_is_not_found(client):
    _with_session(StubSession(disclosure=None))
    assert _transition(client, {"to_status": "in_review"}).status_code == 404


@pytest.mark.parametrize(
    "current,target",
    [
        ("draft", "in_review"),
        ("in_review", "approved"),
        ("approved", "delivered"),
    ],
)
def test_the_happy_path_transitions_are_allowed(client, current, target):
    _with_session(
        StubSession(disclosure=_disclosure(current), provenance_row=CHAIN_ROW)
    )
    response = _transition(client, {"to_status": target})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == target


@pytest.mark.parametrize(
    "current,target",
    [
        # Skipping compliance entirely — the whole point of the hold.
        ("draft", "approved"),
        ("draft", "delivered"),
        ("in_review", "delivered"),
        # Backwards without a reject.
        ("approved", "in_review"),
        # A delivered disclosure is what the borrower was shown. It has no outgoing edge.
        ("delivered", "approved"),
        ("delivered", "draft"),
        ("delivered", "delivered"),
        # Not in the vocabulary at all.
        ("draft", "rescinded"),
    ],
)
def test_illegal_transitions_are_refused(client, current, target):
    session = _with_session(StubSession(disclosure=_disclosure(current)))
    response = _transition(client, {"to_status": target})
    assert response.status_code == 409
    assert "illegal transition" in response.json()["detail"]
    assert session.commits == 0, "a refused transition must not commit"


def test_delivering_sets_delivered_at_exactly_once(client):
    session = _with_session(
        StubSession(disclosure=_disclosure("approved"), provenance_row=CHAIN_ROW)
    )
    response = _transition(client, {"to_status": "delivered"})

    assert response.status_code == 200
    assert response.json()["delivered_at"] is not None
    assert session.disclosure.delivered_at is not None
    updates = [
        sql for sql, _ in session.statements if sql.strip().upper().startswith("UPDATE")
    ]
    assert len(updates) == 1


@pytest.mark.parametrize("target", ["in_review", "approved"])
def test_no_other_transition_writes_delivered_at(client, target):
    """The DDL check constraint says status='delivered' iff delivered_at IS NOT NULL.
    Setting the timestamp on any other edge would violate it — or worse, satisfy it."""
    current = "draft" if target == "in_review" else "in_review"
    session = _with_session(StubSession(disclosure=_disclosure(current)))
    _transition(client, {"to_status": target})

    assert session.disclosure.delivered_at is None


def test_a_reject_needs_a_routable_reason_code(client):
    """An unrouted reject leaves a regulated document in draft with nobody owning the
    next step."""
    session = _with_session(StubSession(disclosure=_disclosure("in_review")))
    response = _transition(client, {"to_status": "draft"})

    assert response.status_code == 400
    assert "reason_code" in response.json()["detail"]
    assert session.commits == 0


def test_an_unknown_reason_code_is_refused_rather_than_defaulted(client):
    _with_session(StubSession(disclosure=_disclosure("in_review")))
    response = _transition(
        client, {"to_status": "draft", "reason_code": "officer_disliked_it"}
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "reason_code,routed_to",
    [
        ("wording", "assemble"),
        ("formatting", "assemble"),
        ("wrong_terms", "decisioning"),
        ("wrong_rate", "decisioning"),
        ("ineligible", "decisioning"),
    ],
)
def test_a_reject_routes_by_reason_code(client, reason_code, routed_to):
    """Spec D5 stage 6: presentational failures go back to the maker; a wrong loan cannot
    be fixed by re-rendering it, so it exits to decisioning."""
    _with_session(StubSession(disclosure=_disclosure("in_review")))
    response = _transition(client, {"to_status": "draft", "reason_code": reason_code})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "draft"
    assert body["routed_to"] == routed_to


def test_delivery_after_the_loan_is_boarded_is_a_timing_violation(client):
    """Reg Z 1026.17(b): the disclosure is made before consummation. A boarded loan is
    this system's consummation event."""
    session = _with_session(
        StubSession(
            disclosure=_disclosure("approved"),
            consummated=True,
            provenance_row=CHAIN_ROW,
        )
    )
    response = _transition(client, {"to_status": "delivered"})

    assert response.status_code == 409
    assert "TILA timing" in response.json()["detail"]
    assert session.commits == 0
    assert not any(
        sql.strip().upper().startswith("UPDATE") for sql, _ in session.statements
    )


def test_a_concurrent_transition_loses_rather_than_double_applying(client):
    """Two officers acting at once: the guarded UPDATE matches zero rows for the loser,
    which must surface as a conflict instead of an exception from the freeze trigger."""
    _with_session(
        StubSession(
            disclosure=_disclosure("approved"),
            update_rowcount=0,
            provenance_row=CHAIN_ROW,
        )
    )
    response = _transition(client, {"to_status": "delivered"})

    assert response.status_code == 409
    assert "concurrently" in response.json()["detail"]


def test_the_transition_is_guarded_on_the_status_it_read(client):
    session = _with_session(StubSession(disclosure=_disclosure("draft")))
    _transition(client, {"to_status": "in_review"})

    sql = next(
        sql for sql, _ in session.statements if sql.strip().upper().startswith("UPDATE")
    )
    assert "disclosures.status = " in sql, (
        "UPDATE must be conditional on the old status"
    )


def test_delivered_has_no_outgoing_transition_at_all():
    """Asserted on the table itself, not only through the routes: an empty set here is
    what makes the freeze trigger a backstop rather than the only guard."""
    assert router_mod.LEGAL_TRANSITIONS["delivered"] == set()


def test_the_status_vocabulary_matches_the_ddl_check_constraint():
    """A status the router can write but the CHECK constraint rejects would be a 500 in
    production and a green test suite here."""
    from pathlib import Path

    schema = (
        Path(__file__).resolve().parents[3] / "db" / "init" / "001_schema.sql"
    ).read_text()
    assert "CHECK (status IN ('draft', 'in_review', 'approved', 'delivered'))" in schema

    reachable = set(router_mod.LEGAL_TRANSITIONS) | {
        target
        for targets in router_mod.LEGAL_TRANSITIONS.values()
        for target in targets
    }
    assert reachable == {"draft", "in_review", "approved", "delivered"}


def test_the_orm_metadata_has_no_unresolvable_foreign_keys():
    """SQLAlchemy resolves every FK in the metadata when it sorts tables for a flush.

    `applications` and `decision_events` are owned by other services and are not mapped
    here, so a ForeignKey pointing at them raises NoReferencedTableError on the first
    INSERT — a 500 on the disclosure write path that no stub-session test can see, because
    nothing in this suite flushes. Found by the `make up` smoke; asserted here so it stays
    found. The constraints themselves live in the DDL, where the database enforces them.
    """
    for table in models.Base.metadata.tables.values():
        for foreign_key in table.foreign_keys:
            foreign_key.column  # resolves, or raises NoReferencedTableError


def test_delivery_refuses_an_incomplete_provenance_chain(client):
    """The pipeline gates on this at stage 5 and the UI disables the button, but the
    lifecycle endpoint is reachable without either. Delivery is the irreversible step and
    the row freezes the moment it lands, so the server must enforce it too."""
    session = _with_session(
        StubSession(
            disclosure=_disclosure("approved"),
            provenance_row={**CHAIN_ROW, "application_id": None, "applicant_id": None},
        )
    )
    response = _transition(client, {"to_status": "delivered"})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "provenance chain is incomplete" in detail
    assert "application_id" in detail and "applicant_id" in detail
    assert session.commits == 0
    assert not any(
        sql.strip().upper().startswith("UPDATE") for sql, _ in session.statements
    )


def test_delivery_proceeds_when_the_chain_is_whole(client):
    session = _with_session(
        StubSession(disclosure=_disclosure("approved"), provenance_row=CHAIN_ROW)
    )
    response = _transition(client, {"to_status": "delivered"})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "delivered"
    assert session.commits == 1


@pytest.mark.parametrize("target", ["in_review", "approved", "draft"])
def test_only_delivery_pays_for_the_chain_check(client, target):
    """The check is a query; running it on every transition would cost a read per click
    for a rule that only matters at the irreversible step."""
    current = {"in_review": "draft", "approved": "in_review", "draft": "in_review"}[
        target
    ]
    session = _with_session(
        StubSession(disclosure=_disclosure(current), provenance_row=CHAIN_ROW)
    )
    _transition(
        client,
        {"to_status": target, "reason_code": "wording" if target == "draft" else None},
    )

    assert not any("v_disclosure_provenance" in sql for sql, _ in session.statements)
