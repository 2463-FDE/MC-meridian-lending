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
    assert state["inserts"] == 1  # retry replayed the offer, no second regulated disclosure


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
        json={"application_id": 7, "principal": 15000, "term_months": 36, "annual_rate": 7.99},
        headers={"X-Internal-Service": TOKEN},
    )
    # retry after a rate/principal drift -- much larger loan at a much higher rate
    retry = client.post(
        "/offers",
        json={"application_id": 7, "principal": 30000, "term_months": 12, "annual_rate": 19.99},
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
            raise pg_errors.UniqueViolation("duplicate key value violates uq_offers_app")
        return []

    monkeypatch.setattr(offers_router.db, "query", _q)
    resp = TestClient(app).post("/offers", json=BODY, headers={"X-Internal-Service": TOKEN})
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
            raise pg_errors.UniqueViolation("duplicate key value violates uq_offers_app")
        return []

    monkeypatch.setattr(offers_router.db, "query", _q)
    resp = TestClient(app, raise_server_exceptions=False).post(
        "/offers", json=BODY, headers={"X-Internal-Service": TOKEN}
    )
    assert resp.status_code == 409
