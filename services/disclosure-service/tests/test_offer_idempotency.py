"""Offer generation is idempotent per application (PR review).

create_offer persists a regulated TILA/Reg-Z offers row. A borrower double-click, a browser
retry, or a gateway timeout after the downstream insert must NOT persist a second disclosure
for one application: create_offer reuses the existing offer, and a concurrent loser catches
the uq_offers_app UniqueViolation and replays the winner's offer instead of inserting again.
Mirrors accept_offer's idempotent loan boarding on the origination side.
"""

from app.main import app
from app.routers import offers as offers_router
from fastapi.testclient import TestClient
from psycopg2 import errors as pg_errors

TOKEN = "test-internal-token"
BODY = {"application_id": 7, "principal": 15000, "term_months": 36, "annual_rate": 7.99}


def test_offer_generation_idempotent_on_retry(monkeypatch):
    # Two identical /offers calls (the lost-response retry): the second must replay the first
    # offer, and only ONE offers row is ever inserted.
    monkeypatch.setattr(offers_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    state = {"offer_id": None, "inserts": 0}

    def _q(sql, params=None):
        s = sql.strip().upper()
        if s.startswith("SELECT ID FROM OFFERS"):
            return [{"id": state["offer_id"]}] if state["offer_id"] is not None else []
        if s.startswith("INSERT INTO OFFERS"):
            state["inserts"] += 1
            state["offer_id"] = 501
            return [{"id": 501}]
        return []

    monkeypatch.setattr(offers_router.db, "query", _q)
    client = TestClient(app)

    r1 = client.post("/offers", json=BODY, headers={"X-Internal-Service": TOKEN})
    r2 = client.post("/offers", json=BODY, headers={"X-Internal-Service": TOKEN})

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["offer_id"] == r2.json()["offer_id"] == 501
    assert state["inserts"] == 1  # retry replayed the offer, no second regulated disclosure


def test_offer_concurrent_race_replays_winners_offer(monkeypatch):
    # Two concurrent creates: the pre-check misses for both, the loser's INSERT hits the
    # uq_offers_app UniqueViolation and must replay the winner's offer, never insert a second.
    monkeypatch.setattr(offers_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    calls = {"select": 0}

    def _q(sql, params=None):
        s = sql.strip().upper()
        if s.startswith("SELECT ID FROM OFFERS"):
            calls["select"] += 1
            # first (pre-insert) check misses; the post-conflict lookup finds the winner
            return [] if calls["select"] == 1 else [{"id": 777}]
        if s.startswith("INSERT INTO OFFERS"):
            raise pg_errors.UniqueViolation("duplicate key value violates uq_offers_app")
        return []

    monkeypatch.setattr(offers_router.db, "query", _q)
    resp = TestClient(app).post("/offers", json=BODY, headers={"X-Internal-Service": TOKEN})
    assert resp.status_code == 200
    assert resp.json()["offer_id"] == 777  # the winner's offer, not a second insert


def test_offer_conflict_without_retrievable_offer_is_409(monkeypatch):
    # Defensive: a UniqueViolation whose winner cannot then be read back surfaces a 409, not a
    # 500 (mirrors accept_offer's boarding-conflict handling).
    monkeypatch.setattr(offers_router.config, "INTERNAL_SERVICE_TOKEN", TOKEN)

    def _q(sql, params=None):
        s = sql.strip().upper()
        if s.startswith("SELECT ID FROM OFFERS"):
            return []  # never finds an offer, even after the conflict
        if s.startswith("INSERT INTO OFFERS"):
            raise pg_errors.UniqueViolation("duplicate key value violates uq_offers_app")
        return []

    monkeypatch.setattr(offers_router.db, "query", _q)
    resp = TestClient(app, raise_server_exceptions=False).post(
        "/offers", json=BODY, headers={"X-Internal-Service": TOKEN}
    )
    assert resp.status_code == 409
