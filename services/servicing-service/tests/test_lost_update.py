"""The lost update on `balances` — EXPECTED TO FAIL until D3 is fixed.

This test documents a defect; it is not a red/green pair for `make prove`. It fails
today, on purpose, the same way `test_money.py::test_float_payment_drift` does, and it
passes only once the mutation becomes atomic. Do not "fix" it by asserting the wrong
figure — the wrong figure is the defect.

The defect (`app/balance.py:23-32`): apply_payment reads the balance, computes the new
value in Python, then writes it back, as two separate round-trips on a connection with
autocommit on (`app/db.py:9-14`). No `SELECT ... FOR UPDATE`, no version column, no
transaction around the pair. Two concurrent applies to the same loan therefore read the
same opening figure and overwrite one another, so captured money never reaches the loan.

Measured against the live stack on 2026-08-02 (`scripts/repro_double_charge.py`): one
$100.00 intent sent eight ways concurrently produced 8 `payments` rows, $800.00 captured
and $600.00 credited — $200.00 taken and never applied, every response 200.

This test reproduces the same defect deterministically rather than by racing threads and
hoping: a barrier inside the stubbed SELECT holds both callers until both have READ, so
the interleave is read -> read -> write -> write on every run. That is the honest shape of
the failure and it cannot flake.

The client brief's own example — a payment concurrent with a fee waiver — does NOT
reproduce this: apply_payment writes `balance` (balance.py:27-30) and waive_fee writes
`past_due` (balance.py:51-53), different columns. The collision is same-column, which is
what this test drives. See `docs/servicing-money-comprehension-week6.md` Q2.
"""

import threading

import pytest

from app import balance

OPENING_BALANCE = 500.0
PAYMENT_A = 100.0
PAYMENT_B = 200.0
CORRECT_FINAL_BALANCE = OPENING_BALANCE - PAYMENT_A - PAYMENT_B  # 200.0


@pytest.fixture
def interleaved_db(monkeypatch):
    """Stub `app.db.query` so both callers finish READing before either WRITEs.

    Separate callers, one shared row — the same situation two concurrent HTTP requests
    are in, since `db.py` hands every caller the same module-level connection.
    """

    class Store:
        def __init__(self, readers):
            self.balance = OPENING_BALANCE
            self.writes = []
            self._both_have_read = threading.Barrier(readers)
            self._lock = threading.Lock()

        def query(self, sql, params=None):
            upper = sql.upper()
            if upper.strip().startswith("SELECT") and "BALANCE" in upper:
                with self._lock:
                    value = self.balance
                # Hold here until every caller has read. Times out rather than hanging
                # the suite if the code under test stops reading before it writes.
                self._both_have_read.wait(timeout=5)
                return [{"balance": value}]
            if "UPDATE BALANCES" in upper and "SET BALANCE" in upper:
                with self._lock:
                    self.balance = params[0]
                    self.writes.append(params[0])
                return []
            return []

    store = Store(readers=2)
    monkeypatch.setattr(balance.db, "query", store.query)
    return store


def test_concurrent_applies_on_the_same_column_lose_an_update(interleaved_db):
    errors = []

    def apply(amount):
        try:
            balance.apply_payment(1, amount)
        except (
            Exception
        ) as exc:  # a barrier timeout must fail loudly, not silently pass
            errors.append(exc)

    threads = [
        threading.Thread(target=apply, args=(PAYMENT_A,)),
        threading.Thread(target=apply, args=(PAYMENT_B,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"apply_payment raised: {errors!r}"
    assert not any(t.is_alive() for t in threads), "a thread did not finish"
    assert len(interleaved_db.writes) == 2, (
        f"expected two writes, got {interleaved_db.writes}"
    )

    captured = PAYMENT_A + PAYMENT_B
    credited = OPENING_BALANCE - interleaved_db.balance
    assert interleaved_db.balance == pytest.approx(CORRECT_FINAL_BALANCE), (
        f"lost update (debt D3): two concurrent applies of ${PAYMENT_A:.2f} and "
        f"${PAYMENT_B:.2f} against an opening balance of ${OPENING_BALANCE:.2f} left the "
        f"balance at ${interleaved_db.balance:.2f}, not ${CORRECT_FINAL_BALANCE:.2f}. "
        f"${captured:.2f} captured, ${credited:.2f} credited, "
        f"${captured - credited:.2f} taken and never applied. Both callers read "
        f"${OPENING_BALANCE:.2f} and the last writer won: writes were "
        f"{interleaved_db.writes}. Fix is the atomic UPDATE specified in ADR 0013."
    )
