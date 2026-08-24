"""D19 test vectors (docs/spec-payments-week5.md, the R table).

The defect: a retried or double-clicked POST inserted a second payments row and
charged the card again. Measured against the live stack 2026-08-02 -- one $100 intent
sent eight ways produced 8 rows, $800.00 captured, every response 200.

These drive `charge()` against a fake `payments` table that MODELS THE PARTIAL UNIQUE
INDEX: an INSERT carrying an idempotency_key already held by a row returns zero rows,
exactly as `ON CONFLICT ... DO NOTHING` does, and a NULL key never conflicts. That is
the whole enforcement point, so a fake that did not model it would prove nothing.

What this file cannot prove is that the real index behaves that way, or that the
shipped `ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL` string can
infer it as an arbiter -- a bare conflict target raises at runtime instead. That is
vectors R-DDL/R-DDL2 in test_idempotency_ddl_live.py, which need a real Postgres.
"""

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app import payments

_TERMINAL = ("captured", "failed", "settled", "returned")


class FakePayments:
    """The payments table, with the partial unique index on idempotency_key."""

    def __init__(self):
        self.rows = []
        self._next_id = 1
        self._lock = threading.Lock()
        self.now = datetime.now(timezone.utc)
        self.claim_barrier = None  # set by the concurrency vector

    def query(self, sql, params=None):
        if sql.startswith("INSERT INTO payments"):
            return self._insert(params)
        if sql.startswith("UPDATE payments SET idempotency_key = NULL"):
            return self._retire(params[0])
        if sql.startswith("SELECT id, loan_id, amount"):
            return self._read(params[0])
        if sql.startswith("UPDATE payments SET status"):
            return self._finalize(params)
        raise AssertionError(f"unexpected SQL: {sql}")

    def _insert(self, params):
        (
            loan_id,
            pan,
            amount,
            amount_minor,
            method,
            key,
            ttl_hours,
            fingerprint,
            processor_key,
        ) = params
        if self.claim_barrier is not None:
            # Hold every caller until all of them have built their INSERT, so the
            # race is real on every run rather than dependent on thread timing.
            self.claim_barrier.wait()
        with self._lock:
            # The partial unique index: a non-NULL key already present conflicts.
            if key is not None and any(r["idempotency_key"] == key for r in self.rows):
                return []
            row = {
                "id": self._next_id,
                "loan_id": loan_id,
                "pan": pan,
                "amount": amount,
                "amount_minor": amount_minor,
                "method": method,
                "status": "processing",
                "idempotency_key": key,
                "request_fingerprint": fingerprint,
                "processor_idempotency_key": processor_key,
                "idempotency_expires_at": self.now + timedelta(hours=ttl_hours),
            }
            self.rows.append(row)
            self._next_id += 1
            return [{"id": row["id"]}]

    def _retire(self, key):
        with self._lock:
            for r in self.rows:
                if (
                    r["idempotency_key"] == key
                    and r["idempotency_expires_at"] <= self.now
                    and r["status"] in _TERMINAL
                ):
                    r["idempotency_key"] = None
                    return [{"id": r["id"]}]
            return []

    def _read(self, key):
        for r in self.rows:
            if r["idempotency_key"] == key:
                out = dict(r)
                out["expired"] = r["idempotency_expires_at"] <= self.now
                return [out]
        return []

    def _finalize(self, params):
        status, payment_id = params
        for r in self.rows:
            if r["id"] == payment_id:
                r["status"] = status
        return []

    def age_out(self, key):
        """Push a key's window into the past, as the retention window passing does."""
        for r in self.rows:
            if r["idempotency_key"] == key:
                r["idempotency_expires_at"] = self.now - timedelta(seconds=1)


@pytest.fixture
def fake_db(monkeypatch):
    db = FakePayments()
    monkeypatch.setattr(payments.db, "query", db.query)
    monkeypatch.setattr(payments, "INTERNAL_SERVICE_TOKEN", "sekret")
    processor_calls = []

    class _Resp:
        status_code = 200

    def _post(url, json=None, headers=None, timeout=None):
        processor_calls.append(json)
        return _Resp()

    monkeypatch.setattr(payments.httpx, "post", _post)
    db.applies = processor_calls
    return db


def _key():
    return str(uuid.uuid4())


def _charge(key, *, loan_id=4471, amount=250.00, pan="4111111111111111", method="card"):
    return payments.charge(
        loan_id=loan_id,
        pan=pan,
        amount=amount,
        method=method,
        idempotency_key=key,
    )


def test_r1_same_key_same_body_charges_once_and_replays(fake_db):
    """R1: one row, one capture; the second call returns the first's result."""
    key = _key()
    first = _charge(key)
    second = _charge(key)

    assert len(fake_db.rows) == 1, "a retry must not insert a second payments row"
    assert len(fake_db.applies) == 1, "a retry must not capture a second time"
    assert second["idempotency"] == payments.REPLAY
    assert second["payment_id"] == first["payment_id"]
    assert second["status"] == "captured"


def test_r1b_replay_after_captured_unapplied_replays_the_unapplied_state(
    fake_db, monkeypatch
):
    """B1: the first call's apply to servicing fails -- captured_unapplied, 424.
    The row's FINALIZE write must persist that actual status, not a hardcoded
    "captured": a replay reconstructs its response from the row (see charge()'s
    REPLAY branch), so a wrongly-persisted "captured" row turns the replay into a
    200 reporting the full amount applied, silently losing the 424 signal.
    """
    key = _key()

    class _Denied:
        status_code = 403

    monkeypatch.setattr(payments.httpx, "post", lambda *a, **k: _Denied())

    first = _charge(key)
    assert first["status"] == "captured_unapplied"
    assert first["applied_amount"] == 0.0

    second = _charge(key)
    assert second["idempotency"] == payments.REPLAY
    assert second["status"] == "captured_unapplied", (
        "a replay of a request whose apply failed must not report a captured success"
    )
    assert second["applied_amount"] == 0.0


def test_expired_captured_unapplied_never_retires_or_double_charges(
    fake_db, monkeypatch
):
    """_RETIRE_SQL deliberately excludes captured_unapplied from the statuses that
    release a key (see the comment above it): the card was actually charged and the
    balance was not, so retiring the key on TTL expiry would let a later retry mint a
    SECOND real charge under the same logical intent while the first sits unresolved.
    Past the TTL, the row must still hold its key and still replay 424/0 -- not go
    IN_FLIGHT (which would 409 forever) and not silently free the key for a new
    charge (the double-charge D19 exists to prevent)."""
    key = _key()

    class _Denied:
        status_code = 403

    monkeypatch.setattr(payments.httpx, "post", lambda *a, **k: _Denied())
    first = _charge(key)
    assert first["status"] == "captured_unapplied"

    fake_db.age_out(key)

    second = _charge(key)
    assert second["idempotency"] == payments.REPLAY
    assert second["status"] == "captured_unapplied"
    assert second["applied_amount"] == 0.0
    assert len(fake_db.rows) == 1, "an expired unapplied capture must not free its key"
    assert fake_db.rows[0]["idempotency_key"] == key


def test_r2_two_simultaneous_identical_requests_capture_once(fake_db):
    """R2: the vector `make prove` runs. Exactly one caller claims the key.

    Both threads are held at a barrier INSIDE the claim insert until both have built
    it, so the interleave is claim/claim on every run and cannot flake.
    """
    key = _key()
    fake_db.claim_barrier = threading.Barrier(2)
    results = {}

    def _run(n):
        try:
            results[n] = _charge(key)
        except Exception as exc:  # surfaced by the assertions below
            results[n] = exc

    threads = [threading.Thread(target=_run, args=(n,)) for n in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(fake_db.rows) == 1, "concurrent identical requests wrote two rows"
    assert len(fake_db.applies) == 1, "the card was captured twice"
    outcomes = sorted(r["idempotency"] for r in results.values())
    # The loser cannot proceed to capture: it either replays the winner's finished
    # row or is told the winner is still in flight. Never a second capture.
    assert outcomes[0] == payments.CLAIMED
    assert outcomes[1] in (payments.REPLAY, payments.IN_FLIGHT)


def test_r3_same_key_different_amount_is_refused(fake_db):
    """R3: a reused key carrying a different payload is a client defect -> 422."""
    key = _key()
    _charge(key, amount=250.00)
    second = _charge(key, amount=500.00)

    assert second["idempotency"] == payments.FINGERPRINT_MISMATCH
    assert len(fake_db.rows) == 1
    assert len(fake_db.applies) == 1, "a mismatched retry must not capture"


def test_r3b_same_key_different_card_is_refused(fake_db):
    """R3b: a changed INSTRUMENT is a changed payload, not a retry.

    Adapted from the spec, which fingerprints `card_token` -- that column does not
    exist until tokenization (D13), so the fingerprint covers a hash of the PAN. The
    behaviour under test is the spec's: same key + different card -> 422, no capture.
    """
    key = _key()
    _charge(key, pan="4111111111111111")
    second = _charge(key, pan="5555555555554444")

    assert second["idempotency"] == payments.FINGERPRINT_MISMATCH
    assert len(fake_db.rows) == 1
    assert len(fake_db.applies) == 1


@pytest.mark.xfail(
    strict=True,
    reason="R3c is unsatisfiable until D4b adds bank_token: this codebase has no "
    "bank instrument field at all, so two ACH submissions reusing one key against "
    "DIFFERENT accounts carry identical (loan_id, amount, method) and hash equal. "
    "Left red on purpose rather than deleted -- deleting it would retire a real gap.",
)
def test_r3c_same_key_different_bank_account_is_refused(fake_db):
    key = _key()
    _charge(key, pan=None, method="ach")
    second = _charge(key, pan=None, method="ach")
    assert second["idempotency"] == payments.FINGERPRINT_MISMATCH


def test_r6_expired_key_on_a_finished_payment_is_a_new_payment(fake_db):
    """R6: past the window a late retry is a NEW payment, by definition.

    The first row survives with its key retired to NULL -- the key is released, the
    payment is not deleted -- and the second row carries a DIFFERENT processor key, so
    a processor whose own retention outlives ours cannot collapse the two.
    """
    key = _key()
    first = _charge(key)
    fake_db.age_out(key)
    second = _charge(key)

    assert second["idempotency"] == payments.CLAIMED
    assert len(fake_db.rows) == 2
    assert second["payment_id"] != first["payment_id"]
    assert fake_db.rows[0]["idempotency_key"] is None, "the key must be retired"
    assert fake_db.rows[0]["status"] == "captured", "the payment must survive"
    assert (
        fake_db.rows[0]["processor_idempotency_key"]
        != fake_db.rows[1]["processor_idempotency_key"]
    ), "a per-row processor key is what breaks the two-vendor window coupling"


def test_r6c_expired_key_on_an_unfinished_intent_keeps_its_key(fake_db):
    """R6c: an ACH row sits `submitted` for days and outlives the window.

    Releasing its key would free the value for a NEW charge while the original intent
    is still live -- the exact double charge this control closes. So: no new row, no
    capture, and the key stays put.
    """
    key = _key()
    _charge(key)
    fake_db.rows[0]["status"] = "submitted"
    fake_db.age_out(key)

    second = _charge(key)

    assert second["idempotency"] == payments.IN_FLIGHT
    assert len(fake_db.rows) == 1, "an unfinished intent must not spawn a second row"
    assert len(fake_db.applies) == 1
    assert fake_db.rows[0]["idempotency_key"] == key, "the key must NOT be retired"


def test_r7_key_held_by_a_still_processing_row_is_refused(fake_db):
    """R7: exactly one capture happens per key while the first is in flight."""
    key = _key()
    _charge(key)
    fake_db.rows[0]["status"] = "processing"

    second = _charge(key)

    assert second["idempotency"] == payments.IN_FLIGHT
    assert len(fake_db.rows) == 1
    assert len(fake_db.applies) == 1


def test_the_measured_defect_eight_retries_capture_once(fake_db):
    """The 2026-08-02 reproduction, inverted.

    Then: 8 POSTs of $100 under one intent produced 8 rows and captured $800.00.
    Now: one row, one capture, $100.00.
    """
    key = _key()
    for _ in range(8):
        _charge(key, amount=100.00)

    assert len(fake_db.rows) == 1
    assert len(fake_db.applies) == 1
    assert fake_db.rows[0]["amount_minor"] == 10000


def test_amount_minor_does_not_lose_a_cent_to_float(fake_db):
    """float(25000.29) * 100 is 2500028.9999999995; int() would drop a cent.

    The value feeds both the stored amount_minor and the fingerprint, so a truncated
    cent would also make two different amounts hash equal at the boundary.
    """
    assert payments._amount_minor(25000.29) == 2500029
    assert payments._amount_minor(0.1 + 0.2) == 30
    assert payments._amount_minor("250.005") == 25001  # ROUND_HALF_UP, not banker's


def test_a_charge_without_a_key_is_a_programming_error(fake_db):
    """The route refuses a keyless request; reaching charge() without one is a bug."""
    with pytest.raises(ValueError):
        payments.charge(loan_id=1, pan="4111111111111111", amount=1.0)
