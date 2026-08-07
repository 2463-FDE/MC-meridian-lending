"""Offer generation is idempotent per application (PR review).

create_offer persists a regulated TILA/Reg-Z offers row. A borrower double-click, a browser
retry, or a gateway timeout after the downstream insert must NOT persist a second disclosure
for one application: create_offer reuses the existing offer, and a concurrent loser catches
the uq_offers_app UniqueViolation and replays the winner's offer instead of inserting again.
Crucially the replay is built from the PERSISTED offer row, not from the (possibly drifted)
retry request body, so the disclosure returned always equals the one accept_offer will board.
Mirrors accept_offer's idempotent loan boarding on the origination side.
"""

from app.main import app
from app.routers import offers as offers_router
from fastapi.testclient import TestClient
from psycopg2 import errors as pg_errors

TOKEN = "test-internal-token"
BODY = {"application_id": 7, "principal": 15000, "term_months": 36, "annual_rate": 7.99}


def _persisted_from_insert_params(offer_id, params):
    """Model a stored offers row from what create_offer's INSERT actually persisted."""
    return {
        "id": offer_id,
        "apr": params[1],
        "finance_charge": params[2],
        "monthly_payment": params[3],
        "amount_financed": params[4],
        "total_of_payments": params[5],
        "decision_event_id": params[6],
    }


def test_offer_generation_idempotent_on_retry(monkeypatch):
    # Two identical /offers calls (the lost-response retry): the second replays the first
    # offer, and only ONE offers row is ever inserted.
    monkeypatch.setattr(offers_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    state = {"offer": None, "inserts": 0}

    def _q(sql, params=None):
        s = sql.strip().upper()
        if s.startswith("SELECT") and "FROM OFFERS" in s:
            return [state["offer"]] if state["offer"] is not None else []
        if s.startswith("INSERT INTO OFFERS"):
            state["inserts"] += 1
            state["offer"] = _persisted_from_insert_params(501, params)
            return [{"id": 501}]
        return []

    monkeypatch.setattr(offers_router.db, "query", _q)
    client = TestClient(app)

    r1 = client.post("/offers", json=BODY, headers={"X-Internal-Service": TOKEN})
    r2 = client.post("/offers", json=BODY, headers={"X-Internal-Service": TOKEN})

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["offer_id"] == r2.json()["offer_id"] == 501
    assert (
        state["inserts"] == 1
    )  # retry replayed the offer, no second regulated disclosure


def test_replay_returns_persisted_terms_not_drifted_request(monkeypatch):
    # The reviewer's drift case: the FIRST POST persists an offer; a SECOND POST with a
    # DIFFERENT rate/principal (policy-rate deploy, term correction, caller drift) must replay
    # the PERSISTED offer's terms under the same offer_id -- never the freshly computed drifted
    # terms -- because accept_offer boards from the stored row.
    monkeypatch.setattr(offers_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    state = {"offer": None}

    def _q(sql, params=None):
        s = sql.strip().upper()
        if s.startswith("SELECT") and "FROM OFFERS" in s:
            return [state["offer"]] if state["offer"] is not None else []
        if s.startswith("INSERT INTO OFFERS"):
            state["offer"] = _persisted_from_insert_params(501, params)
            return [{"id": 501}]
        return []

    monkeypatch.setattr(offers_router.db, "query", _q)
    client = TestClient(app)

    first = client.post(
        "/offers",
        json={
            "application_id": 7,
            "principal": 15000,
            "term_months": 36,
            "annual_rate": 7.99,
        },
        headers={"X-Internal-Service": TOKEN},
    )
    # retry after a rate/principal drift -- much larger loan at a much higher rate
    retry = client.post(
        "/offers",
        json={
            "application_id": 7,
            "principal": 30000,
            "term_months": 12,
            "annual_rate": 19.99,
        },
        headers={"X-Internal-Service": TOKEN},
    )

    assert first.status_code == 200 and retry.status_code == 200
    assert retry.json()["offer_id"] == first.json()["offer_id"] == 501
    # the replay discloses the PERSISTED terms, not the drifted retry's computed ones
    assert retry.json()["apr"] == first.json()["apr"]
    assert retry.json()["monthly_payment"] == first.json()["monthly_payment"]
    assert (
        retry.json()["disclosure"]["amount_financed"]
        == first.json()["disclosure"]["amount_financed"]
    )
    # and it is NOT the drifted 30000/19.99 computation
    drifted = offers_router.offer_mod.build_offer(30000, 19.99, 12)
    assert retry.json()["apr"] != drifted["apr"]


def test_offer_concurrent_race_replays_winners_offer(monkeypatch):
    # Two concurrent creates: the pre-check misses for both, the loser's INSERT hits the
    # uq_offers_app UniqueViolation and must replay the winner's PERSISTED offer, never insert
    # a second.
    monkeypatch.setattr(offers_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    winner = {
        "id": 777,
        "apr": 8.5,
        "finance_charge": 1000.0,
        "monthly_payment": 400.0,
        "amount_financed": 14000.0,
        "total_of_payments": 14400.0,
    }
    calls = {"select": 0}

    def _q(sql, params=None):
        s = sql.strip().upper()
        if s.startswith("SELECT") and "FROM OFFERS" in s:
            calls["select"] += 1
            # first (pre-insert) check misses; the post-conflict lookup finds the winner
            return [] if calls["select"] == 1 else [winner]
        if s.startswith("INSERT INTO OFFERS"):
            raise pg_errors.UniqueViolation(
                "duplicate key value violates uq_offers_app"
            )
        return []

    monkeypatch.setattr(offers_router.db, "query", _q)
    resp = TestClient(app).post(
        "/offers", json=BODY, headers={"X-Internal-Service": TOKEN}
    )
    assert resp.status_code == 200
    assert resp.json()["offer_id"] == 777  # the winner's offer, not a second insert
    assert resp.json()["apr"] == 8.5  # winner's PERSISTED terms, not the request's


def test_offer_conflict_without_retrievable_offer_is_409(monkeypatch):
    # Defensive: a UniqueViolation whose winner cannot then be read back surfaces a 409, not a
    # 500 (mirrors accept_offer's boarding-conflict handling).
    monkeypatch.setattr(offers_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)

    def _q(sql, params=None):
        s = sql.strip().upper()
        if s.startswith("SELECT") and "FROM OFFERS" in s:
            return []  # never finds an offer, even after the conflict
        if s.startswith("INSERT INTO OFFERS"):
            raise pg_errors.UniqueViolation(
                "duplicate key value violates uq_offers_app"
            )
        return []

    monkeypatch.setattr(offers_router.db, "query", _q)
    resp = TestClient(app, raise_server_exceptions=False).post(
        "/offers", json=BODY, headers={"X-Internal-Service": TOKEN}
    )
    assert resp.status_code == 409


class TestAuthorizingDecisionEvent:
    """The offer records WHICH decision authorized it, at creation.

    Left to disclosure time, the provenance edge was closed from whichever decision event
    was latest by then. `decision_events` is append-only and `uq_offers_app` makes the offer
    permanent, so a re-decision between the offer and its disclosure re-parented the offer
    to an event that did not produce its terms — and `v_disclosure_provenance`, which joins
    the decision through `offers.decision_event_id`, reported that chain as complete. Wrong
    and silent, rather than partial and flagged.
    """

    def _db(self, monkeypatch, state, decision_app_id=7):
        def _q(sql, params=None):
            s = sql.strip().upper()
            if "FROM DECISION_EVENTS" in s:
                return [{"app_id": decision_app_id}] if decision_app_id else []
            if s.startswith("SELECT") and "FROM OFFERS" in s:
                return [state["offer"]] if state["offer"] is not None else []
            if s.startswith("INSERT INTO OFFERS"):
                state["inserts"] += 1
                state["offer"] = _persisted_from_insert_params(501, params)
                return [{"id": 501}]
            return []

        monkeypatch.setattr(offers_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
        monkeypatch.setattr(offers_router.db, "query", _q)
        return TestClient(app)

    def test_the_authorizing_event_is_written_with_the_offer(self, monkeypatch):
        state = {"offer": None, "inserts": 0}
        client = self._db(monkeypatch, state)
        resp = client.post(
            "/offers",
            json={**BODY, "decision_event_id": 42},
            headers={"X-Internal-Service": TOKEN},
        )
        assert resp.status_code == 200, resp.text
        assert state["offer"]["decision_event_id"] == 42
        assert resp.json()["decision_event_id"] == 42

    def test_a_replay_echoes_the_persisted_offers_event(self, monkeypatch):
        """The retry is answered with the event the STORED offer cites, so the disclosure
        that follows cites it too — not whichever event the retry happened to carry."""
        state = {"offer": None, "inserts": 0}
        client = self._db(monkeypatch, state)
        client.post(
            "/offers",
            json={**BODY, "decision_event_id": 42},
            headers={"X-Internal-Service": TOKEN},
        )
        retry = client.post(
            "/offers",
            json={**BODY, "decision_event_id": 99},
            headers={"X-Internal-Service": TOKEN},
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["decision_event_id"] == 42
        assert state["inserts"] == 1

    def test_a_decision_event_from_another_application_is_refused(self, monkeypatch):
        state = {"offer": None, "inserts": 0}
        client = self._db(monkeypatch, state, decision_app_id=999)
        resp = client.post(
            "/offers",
            json={**BODY, "decision_event_id": 42},
            headers={"X-Internal-Service": TOKEN},
        )
        assert resp.status_code == 409
        assert "does not belong" in resp.json()["detail"]
        assert state["inserts"] == 0

    def test_a_decision_event_that_does_not_exist_is_refused(self, monkeypatch):
        state = {"offer": None, "inserts": 0}
        client = self._db(monkeypatch, state, decision_app_id=None)
        resp = client.post(
            "/offers",
            json={**BODY, "decision_event_id": 42},
            headers={"X-Internal-Service": TOKEN},
        )
        assert resp.status_code == 404
        assert state["inserts"] == 0

    def test_an_offer_with_no_decision_event_still_persists(self, monkeypatch):
        """An application decided before `decision_events` existed keeps the old shape: the
        offer carries no edge and `create_disclosure` closes one the same-application way."""
        state = {"offer": None, "inserts": 0}
        client = self._db(monkeypatch, state)
        resp = client.post("/offers", json=BODY, headers={"X-Internal-Service": TOKEN})
        assert resp.status_code == 200, resp.text
        assert state["offer"]["decision_event_id"] is None
        assert resp.json()["decision_event_id"] is None


class TestScheduleRate:
    """The display schedule must amortize at the NOTE rate, never the APR.

    The APR carries the origination fee, so it is above the rate interest actually accrues
    at. Amortizing at it produced a schedule that contradicted the disclosed figures in the
    SAME response: on 15000/7.99/36 the rows summed to 17442.72 against a disclosed total of
    payments of 16919.15, with a 484.52 payment shown against a disclosed 469.98. The
    fresh-insert path always passed the note rate; only the replay/read path did not, so an
    offer showed one schedule when created and another when read back — and the read path is
    what the portal displays. Found by the teeth pass, reproduced against the live stack.
    """

    OFFER_ROW = {
        "id": 190,
        "apr": 10.072,
        "finance_charge": 2369.15,
        "monthly_payment": 469.98,
        "amount_financed": 14550.0,
        "total_of_payments": 16919.15,
    }
    SNAPSHOT = {
        "principal_cents": 1500000,
        "note_rate_pct": "7.99",
        "term_months": 36,
        "fee_pct": "0.03",
    }

    def _response(self, monkeypatch, rows):
        from app.routers import offers as mod

        monkeypatch.setattr(mod.db, "query", lambda *a, **k: rows)
        return mod._offer_response_from_persisted(dict(self.OFFER_ROW), 7303)

    def test_the_schedule_reconciles_with_the_disclosed_payment(self, monkeypatch):
        resp = self._response(monkeypatch, [{"compute_snapshot": self.SNAPSHOT}])
        assert resp.schedule, "expected a rendered schedule"
        assert resp.schedule[0].payment == self.OFFER_ROW["monthly_payment"]

    def test_the_schedule_is_not_amortized_at_the_apr(self, monkeypatch):
        """The exact regression: at the APR the first payment was 484.52."""
        resp = self._response(monkeypatch, [{"compute_snapshot": self.SNAPSHOT}])
        assert resp.schedule[0].payment != 484.52

    def test_the_snapshot_supplies_principal_rather_than_an_inversion(
        self, monkeypatch
    ):
        """`compute_snapshot` records the principal actually used, so the schedule no
        longer depends on the current fee rate matching the one the offer was priced at."""
        from app.routers import offers as mod

        monkeypatch.setattr(
            mod.db, "query", lambda *a, **k: [{"compute_snapshot": self.SNAPSHOT}]
        )
        principal, note_rate = mod._schedule_inputs(dict(self.OFFER_ROW), 14550.0)
        assert principal == 15000.0
        assert note_rate == 7.99

    def test_a_legacy_offer_with_no_disclosure_still_renders(self, monkeypatch):
        """Nothing better exists for a pre-ADR-0012 offer, so the inversion stays as the
        fallback rather than the schedule disappearing."""
        from app.routers import offers as mod

        monkeypatch.setattr(mod.db, "query", lambda *a, **k: [])
        principal, note_rate = mod._schedule_inputs(dict(self.OFFER_ROW), 14550.0)
        assert principal == 15000.0
        assert note_rate == mod._FALLBACK_NOTE_RATE_PCT
