"""The lost update on `balances` — the D3 before-number, now green (ADR 0020).

This test documented a defect and failed on purpose from the day it was written until
the atomic apply landed. Do not "fix" it by asserting a different figure — the figure
it asserts is the correct one, and the wrong figure was the defect.

The defect it drove: `apply_payment` read the balance, computed the new value in Python,
then wrote it back, as two separate round-trips on a connection with autocommit on
(`app/db.py`). No `SELECT ... FOR UPDATE`, no version column, no transaction around the
pair. Two concurrent applies to the same loan therefore read the same opening figure and
overwrote one another, so captured money never reached the loan.

Measured against the live stack on 2026-08-02 (`scripts/repro_double_charge.py`): one
$100.00 intent sent eight ways concurrently produced 8 `payments` rows, $800.00 captured
and $600.00 credited — $200.00 taken and never applied, every response 200.

The fix is one statement whose UPDATE computes from the stored value
(`SET balance = b.balance - ...`), so concurrent callers serialize on the row lock
instead of overwriting each other.

This reproduces the failure deterministically rather than by racing threads and hoping:
a barrier holds both callers inside the statement until both have arrived, so the
interleave is the worst case on every run. It cannot flake.

**The fixture models a database, not a shape.** It implements the read-modify-write
statements AND the atomic statement faithfully, so the outcome is decided by which
statements the code under test chooses to issue — not by the stub being taught to
expect the fix. Revert `balance.apply_payment` to the read-modify-write and this test
goes red again, which is what `make prove` checks.

The two payment ids are deliberately 100 and 200, matching the two amounts in cents/100.
`make prove` rolls the source back to the parent commit, where `apply_payment`'s second
positional parameter was `amount` rather than `payment_id` — so the rolled-back code
receives these ids as dollar amounts and reproduces the ORIGINAL $500 -> $300 lost
update, rather than failing on some unrelated figure.

The client brief's own example — a payment concurrent with a fee waiver — does NOT
reproduce this: apply_payment writes `balance` and waive_fee writes `past_due`,
different columns. The collision is same-column, which is what this test drives. See
`docs/reports/servicing-money-comprehension-week6.md` Q2.
"""

import threading

import pytest

from app import balance

OPENING_BALANCE = 500.0
PAYMENT_A = 100.0
PAYMENT_B = 200.0
CORRECT_FINAL_BALANCE = OPENING_BALANCE - PAYMENT_A - PAYMENT_B  # 200.0

# id == dollars, for the make-prove reason in the module docstring.
PAYMENT_A_ID = int(PAYMENT_A)
PAYMENT_B_ID = int(PAYMENT_B)


@pytest.fixture
def interleaved_db(monkeypatch):
    """Stub `app.db.query` with a small model of the real table.

    Separate callers, one shared row — the same situation two concurrent HTTP requests
    are in, since `db.py` hands every caller the same module-level connection.

    Both statement shapes are implemented honestly:

    * read-modify-write (`SELECT balance` then `UPDATE ... SET balance = %s`): the READ
      answers with the value at the time it runs, then both callers are held at the
      barrier, then each WRITE stores its absolute figure. Last writer wins — the defect.
    * the atomic statement (`WITH ... UPDATE ... SET balance = b.balance - ...`): both
      callers are held at the barrier on ENTRY, so they are genuinely concurrent, and
      then each decrement runs under the row lock and re-reads the committed value.
      This is what Postgres does under READ COMMITTED, and both credits survive.
    """

    class Store:
        def __init__(self, readers):
            self.balance = OPENING_BALANCE
            self.writes = []
            self.applications = {}
            self.payments = {
                PAYMENT_A_ID: {
                    "loan_id": 1,
                    "amount_minor": int(PAYMENT_A * 100),
                    "status": "captured",
                },
                PAYMENT_B_ID: {
                    "loan_id": 1,
                    "amount_minor": int(PAYMENT_B * 100),
                    "status": "captured",
                },
            }
            self._both_have_arrived = threading.Barrier(readers)
            # The row lock. Held for the whole of the atomic statement, which is the
            # property that makes the decrement safe; the read-modify-write path takes
            # it only around each individual round-trip, which is why it loses.
            self._lock = threading.Lock()

        def query(self, sql, params=None):
            upper = sql.upper()

            if "PAYMENT_APPLICATIONS" in upper and upper.strip().startswith("WITH"):
                # Both callers are inside the statement before either commits. Times out
                # rather than hanging the suite if only one caller ever gets here.
                self._both_have_arrived.wait(timeout=5)
                with self._lock:
                    payment = self.payments.get(params["payment_id"])
                    if (
                        payment is None
                        or payment["loan_id"] != params["loan_id"]
                        or payment["amount_minor"] is None
                        or payment["status"] not in ("captured", "settled")
                        or params["payment_id"] in self.applications
                    ):
                        return []
                    self.applications[params["payment_id"]] = {
                        "loan_id": payment["loan_id"],
                        "amount_minor": payment["amount_minor"],
                    }
                    self.balance -= payment["amount_minor"] / 100.0
                    self.writes.append(self.balance)
                    return [
                        {
                            "loan_id": payment["loan_id"],
                            "balance": self.balance,
                            "amount_minor": payment["amount_minor"],
                        }
                    ]

            if "PAYMENT_APPLICATIONS" in upper and upper.strip().startswith("SELECT"):
                prior = self.applications.get(params[0])
                return [prior] if prior else []

            if upper.strip().startswith("SELECT") and "BALANCE" in upper:
                with self._lock:
                    value = self.balance
                # Hold here until every caller has read, so the read-modify-write
                # interleave is read -> read -> write -> write on every run.
                self._both_have_arrived.wait(timeout=5)
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

    def apply(payment_id):
        try:
            balance.apply_payment(1, payment_id)
        except (
            Exception
        ) as exc:  # a barrier timeout must fail loudly, not silently pass
            errors.append(exc)

    threads = [
        threading.Thread(target=apply, args=(PAYMENT_A_ID,)),
        threading.Thread(target=apply, args=(PAYMENT_B_ID,)),
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
        f"{interleaved_db.writes}. Fix is the atomic UPDATE specified in ADR 0020."
    )


def test_both_applications_are_recorded_exactly_once(interleaved_db):
    """The record half of the same run: two concurrent applies leave two rows, and the
    amounts on them are the ones that moved the balance."""

    def apply(payment_id):
        balance.apply_payment(1, payment_id)

    threads = [
        threading.Thread(target=apply, args=(PAYMENT_A_ID,)),
        threading.Thread(target=apply, args=(PAYMENT_B_ID,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert interleaved_db.applications == {
        PAYMENT_A_ID: {"loan_id": 1, "amount_minor": int(PAYMENT_A * 100)},
        PAYMENT_B_ID: {"loan_id": 1, "amount_minor": int(PAYMENT_B * 100)},
    }
