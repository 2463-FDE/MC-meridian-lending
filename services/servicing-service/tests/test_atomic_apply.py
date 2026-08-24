"""D3 / ADR 0020 — the atomic apply, beyond the lost-update before-number.

`test_lost_update.py` owns the concurrency figure. This file owns the rest of what
spec D3(d) requires of the statement: the amount and the loan come out of the payments
row, a replay moves nothing, and every ineligible case writes nothing at all.

The fixture models the table rather than the statement text, same as the other suites
in this service: eligibility is decided from the modelled rows, so a predicate dropped
from the SQL shows up as a test that starts passing something it should refuse.
"""

import threading

import pytest

from app import balance

OPENING_BALANCE = 1000.0


@pytest.fixture
def db(monkeypatch):
    class Store:
        def __init__(self):
            self.balances = {1: OPENING_BALANCE, 2: 250.0}
            self.applications = {}
            self.payments = {
                # loan 1, $100.00, captured — the ordinary eligible case
                7: {"loan_id": 1, "amount_minor": 10000, "status": "captured"},
                # loan 1, $40.00, settled — also credits
                8: {"loan_id": 1, "amount_minor": 4000, "status": "settled"},
                # loan 1, still processing — the capture nobody has confirmed
                9: {"loan_id": 1, "amount_minor": 5000, "status": "processing"},
                # loan 1, ACH submitted — funds not received
                10: {"loan_id": 1, "amount_minor": 5000, "status": "submitted"},
                # loan 1, pre-0018 row on a volume missing 0019's backfill
                11: {"loan_id": 1, "amount_minor": None, "status": "captured"},
                # loan 2's payment — must never credit loan 1
                12: {"loan_id": 2, "amount_minor": 9900, "status": "captured"},
                # loan 3 has no balances row at all
                13: {"loan_id": 3, "amount_minor": 3300, "status": "captured"},
            }
            self.statements = []
            self._lock = threading.Lock()

        def query(self, sql, params=None):
            self.statements.append(" ".join(sql.split()))
            upper = sql.upper()
            if "PAYMENT_APPLICATIONS" in upper and upper.strip().startswith("WITH"):
                with self._lock:
                    payment = self.payments.get(params["payment_id"])
                    if (
                        payment is None
                        or payment["loan_id"] != params["loan_id"]
                        or payment["amount_minor"] is None
                        or payment["status"] not in ("captured", "settled")
                        # the JOIN to balances
                        or payment["loan_id"] not in self.balances
                        # ON CONFLICT (payment_id) DO NOTHING
                        or params["payment_id"] in self.applications
                    ):
                        return []
                    self.applications[params["payment_id"]] = {
                        "loan_id": payment["loan_id"],
                        "amount_minor": payment["amount_minor"],
                    }
                    self.balances[payment["loan_id"]] -= payment["amount_minor"] / 100.0
                    return [
                        {
                            "loan_id": payment["loan_id"],
                            "balance": self.balances[payment["loan_id"]],
                            "amount_minor": payment["amount_minor"],
                        }
                    ]
            if "PAYMENT_APPLICATIONS" in upper and upper.strip().startswith("SELECT"):
                prior = self.applications.get(params[0])
                return [prior] if prior else []
            if upper.strip().startswith("SELECT BALANCE"):
                loan_id = params[0]
                if loan_id not in self.balances:
                    return []
                return [{"balance": self.balances[loan_id]}]
            return []

    store = Store()
    monkeypatch.setattr(balance.db, "query", store.query)
    return store


def test_the_amount_comes_from_the_payment_row(db):
    new_balance, moved = balance.apply_payment(1, 7)

    assert moved is True
    assert new_balance == pytest.approx(900.0)
    assert db.applications[7]["amount_minor"] == 10000


def test_a_settled_payment_also_credits(db):
    new_balance, moved = balance.apply_payment(1, 8)

    assert moved is True
    assert new_balance == pytest.approx(960.0)


def test_a_replay_moves_the_balance_once(db):
    first, first_moved = balance.apply_payment(1, 7)
    second, second_moved = balance.apply_payment(1, 7)

    assert first_moved is True
    assert second_moved is False, "the second call must not credit again"
    assert first == pytest.approx(900.0)
    assert second == pytest.approx(900.0)
    assert db.balances[1] == pytest.approx(900.0)
    assert len(db.applications) == 1


@pytest.mark.parametrize(
    "payment_id,why",
    [
        pytest.param(9, "processing is a capture nobody confirmed", id="processing"),
        pytest.param(10, "an ACH submission is not funds received", id="ach_submitted"),
        pytest.param(11, "amount_minor is NULL on a pre-0018 row", id="null_minor"),
        pytest.param(99, "no such payment", id="absent"),
    ],
)
def test_an_ineligible_payment_writes_nothing(db, payment_id, why):
    with pytest.raises(balance.PaymentNotApplicable) as exc:
        balance.apply_payment(1, payment_id)

    assert exc.value.reason == "not_applicable", why
    assert db.balances[1] == pytest.approx(OPENING_BALANCE), why
    assert db.applications == {}, why


def test_a_payment_belonging_to_another_loan_does_not_credit_this_one(db):
    # The path's loan_id is a predicate, not an input. Without this, an internal or
    # reaper call applies payment A to loan B and UNIQUE (payment_id) makes it permanent.
    with pytest.raises(balance.PaymentNotApplicable):
        balance.apply_payment(1, 12)

    assert db.balances[1] == pytest.approx(OPENING_BALANCE)
    assert db.balances[2] == pytest.approx(250.0)
    assert db.applications == {}


def test_a_loan_with_no_balances_row_records_no_application(db):
    # Zero rows from the UPDATE half. The application row must not survive a movement
    # that did not happen: UNIQUE (payment_id) would then block the correct retry.
    with pytest.raises(balance.PaymentNotApplicable):
        balance.apply_payment(3, 13)

    assert db.applications == {}


def test_a_payment_already_applied_to_another_loan_is_refused_not_reported_applied(db):
    balance.apply_payment(2, 12)

    with pytest.raises(balance.PaymentNotApplicable) as exc:
        balance.apply_payment(1, 12)

    assert exc.value.reason == "applied_to_another_loan"
    assert db.balances[1] == pytest.approx(OPENING_BALANCE)


def test_eight_concurrent_applies_credit_every_one_of_them(db):
    """The live repro's shape: eight captured payments, applied concurrently, all
    credited. The 2026-08-02 run captured $800.00 and credited $600.00."""
    for payment_id in range(20, 28):
        db.payments[payment_id] = {
            "loan_id": 1,
            "amount_minor": 10000,
            "status": "captured",
        }

    errors = []

    def apply(payment_id):
        try:
            balance.apply_payment(1, payment_id)
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=apply, args=(payment_id,))
        for payment_id in range(20, 28)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"apply_payment raised: {errors!r}"
    assert len(db.applications) == 8
    credited = OPENING_BALANCE - db.balances[1]
    assert credited == pytest.approx(800.0), (
        f"eight concurrent $100.00 applies credited ${credited:.2f}, not $800.00"
    )


# --- the route contract ----------------------------------------------------


def _client(monkeypatch):
    from fastapi.testclient import TestClient

    from app import config
    from app.main import app

    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    return TestClient(app)


def test_the_route_ignores_an_amount_the_caller_sends(db, monkeypatch):
    # `amount` is gone from ApplyPaymentIn. A caller claiming $10,000.00 against a
    # $100.00 payment credits $100.00, because the figure comes off the payments row.
    resp = _client(monkeypatch).post(
        "/accounts/1/apply-payment",
        json={"payment_id": 7, "amount": 10000.0},
        headers={"X-Internal-Service": "sekret"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["new_balance"] == pytest.approx(900.0)
    assert db.applications[7]["amount_minor"] == 10000


def test_the_route_refuses_an_ineligible_payment_with_422(db, monkeypatch):
    resp = _client(monkeypatch).post(
        "/accounts/1/apply-payment",
        json={"payment_id": 9},
        headers={"X-Internal-Service": "sekret"},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "not_applicable"
    assert db.balances[1] == pytest.approx(OPENING_BALANCE)
    assert db.applications == {}


def test_the_route_reports_a_replay_as_moved_false(db, monkeypatch):
    client = _client(monkeypatch)
    headers = {"X-Internal-Service": "sekret"}

    first = client.post(
        "/accounts/1/apply-payment", json={"payment_id": 7}, headers=headers
    )
    second = client.post(
        "/accounts/1/apply-payment", json={"payment_id": 7}, headers=headers
    )

    assert first.json()["moved"] is True
    assert second.status_code == 200
    assert second.json()["moved"] is False, (
        "a replay is a success that credited nothing; the caller must not add it to a "
        "running total"
    )
    assert db.balances[1] == pytest.approx(900.0)
