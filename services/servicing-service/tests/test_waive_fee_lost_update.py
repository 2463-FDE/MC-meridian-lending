"""The lost update on `waive_fee` — D32's first half, now fixed.

Same shape as `test_lost_update.py`'s D3 defect, one column over: `waive_fee` read
`past_due`, subtracted in Python, then wrote the result back as two separate
round-trips on a connection with autocommit on (`app/db.py`). Two concurrent waives
on the same loan therefore read the same opening figure and overwrote one another,
so one waiver never reached `past_due`.

The fix is one statement whose UPDATE computes from the stored value
(`SET past_due = past_due - %s`), so concurrent callers serialize on the row lock
instead of overwriting each other — same fix D3/ADR 0020 applied to `apply_payment`.

This reproduces the failure deterministically rather than by racing threads and
hoping: a barrier holds both callers inside the statement until both have arrived,
so the interleave is the worst case on every run. It cannot flake.

**The fixture models a database, not a shape.** It implements the read-modify-write
statement AND the atomic statement faithfully, so the outcome is decided by which
statement `waive_fee` issues — not by the stub being taught to expect the fix.
Revert `balance.waive_fee` to the read-modify-write and this test goes red again,
which is what `make prove` checks.
"""

import threading

import pytest

from app import balance

OPENING_PAST_DUE = 100.0
WAIVE_A = 30.0
WAIVE_B = 20.0
CORRECT_FINAL_PAST_DUE = OPENING_PAST_DUE - WAIVE_A - WAIVE_B  # 50.0


@pytest.fixture
def interleaved_db(monkeypatch):
    """Stub `app.db.query` with a small model of the `balances` row.

    * read-modify-write (`SELECT past_due` then `UPDATE ... SET past_due = %s`): the
      READ answers with the value at the time it runs, then both callers are held at
      the barrier, then each WRITE stores its absolute figure. Last writer wins.
    * the atomic statement (`UPDATE ... SET past_due = past_due - %s ... RETURNING`):
      both callers are held at the barrier on ENTRY, so they are genuinely
      concurrent, and then each decrement runs under the row lock and re-reads the
      committed value. Both waives survive.
    """

    class Store:
        def __init__(self, readers):
            self.past_due = OPENING_PAST_DUE
            self.writes = []
            self._both_have_arrived = threading.Barrier(readers)
            self._lock = threading.Lock()

        def query(self, sql, params=None):
            upper = sql.upper()

            if "UPDATE BALANCES" in upper and "PAST_DUE = PAST_DUE" in upper:
                # Both callers are inside the statement before either commits.
                self._both_have_arrived.wait(timeout=5)
                with self._lock:
                    self.past_due -= params[0]
                    self.writes.append(self.past_due)
                    return [{"past_due": self.past_due}]

            if upper.strip().startswith("SELECT") and "PAST_DUE" in upper:
                with self._lock:
                    value = self.past_due
                # Hold here until every caller has read, so the read-modify-write
                # interleave is read -> read -> write -> write on every run.
                self._both_have_arrived.wait(timeout=5)
                return [{"past_due": value}]

            if "UPDATE BALANCES" in upper and "SET PAST_DUE" in upper:
                with self._lock:
                    self.past_due = params[0]
                    self.writes.append(params[0])
                return []

            return []

    store = Store(readers=2)
    monkeypatch.setattr(balance.db, "query", store.query)
    return store


def test_concurrent_waives_on_the_same_column_lose_an_update(interleaved_db):
    errors = []

    def waive(amount):
        try:
            balance.waive_fee(1, amount)
        except (
            Exception
        ) as exc:  # a barrier timeout must fail loudly, not silently pass
            errors.append(exc)

    threads = [
        threading.Thread(target=waive, args=(WAIVE_A,)),
        threading.Thread(target=waive, args=(WAIVE_B,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"waive_fee raised: {errors!r}"
    assert not any(t.is_alive() for t in threads), "a thread did not finish"
    assert len(interleaved_db.writes) == 2, (
        f"expected two writes, got {interleaved_db.writes}"
    )

    assert interleaved_db.past_due == pytest.approx(CORRECT_FINAL_PAST_DUE), (
        f"lost update (debt D32): two concurrent waives of ${WAIVE_A:.2f} and "
        f"${WAIVE_B:.2f} against an opening past_due of ${OPENING_PAST_DUE:.2f} left "
        f"past_due at ${interleaved_db.past_due:.2f}, not "
        f"${CORRECT_FINAL_PAST_DUE:.2f}. Both callers read ${OPENING_PAST_DUE:.2f} and "
        f"the last writer won: writes were {interleaved_db.writes}. Fix is the atomic "
        f"UPDATE, same shape as D3/ADR 0020."
    )
