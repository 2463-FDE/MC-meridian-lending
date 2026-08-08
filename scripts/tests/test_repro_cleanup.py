"""Regression test for the repro script's mutation-phase cleanup control flow.

The bug (fixed on this branch): a non-2xx charge mid-run did `sys.exit(...)` *before* the
cleanup path, so a partially-mutated balance and the rows already inserted were left behind
despite cleanup being the default. The fix wraps the mutation phase in try/finally so cleanup
always runs (unless --keep), then reports the failure via sys.exit afterwards.

This test drives `main()` with a charge that fails on the second sequential POST and asserts
cleanup still ran. All network/db functions in the script are module-level, so we swap them
for in-memory fakes and never touch a gateway or a database.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import threading

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "repro_double_charge", _ROOT / "scripts" / "repro_double_charge.py"
)
repro = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(repro)


def test_cleanup_runs_when_a_charge_fails_mid_run(monkeypatch):
    calls = {"cleanup": False}

    # Health check passes; login and the census reads are stubbed to constants.
    monkeypatch.setattr(repro, "api", lambda *a, **k: (200, {"ok": True}))
    monkeypatch.setattr(repro, "login", lambda user, password: "tok")
    monkeypatch.setattr(repro, "balance_of", lambda loan_id: 24800.00)
    monkeypatch.setattr(repro, "payment_ids", lambda loan_id: {1, 2, 3})

    # First charge succeeds, second fails — the partial-mutation case the finding describes.
    charge_codes = iter([(201, {"payment_id": 99}), (500, "processor exploded")])
    monkeypatch.setattr(
        repro, "charge", lambda token, loan_id, amount: next(charge_codes)
    )

    def fake_cleanup(loan_id, ids, restore_to, restore_balance=True):
        calls["cleanup"] = True

    monkeypatch.setattr(repro, "cleanup", fake_cleanup)
    monkeypatch.setattr(sys, "argv", ["repro_double_charge.py"])  # defaults, no --keep

    # The run still reports the failure by exiting non-zero...
    with pytest.raises(SystemExit) as exc:
        repro.main()

    # ...but only AFTER cleanup restored the balance and removed the inserted rows.
    assert calls["cleanup"] is True, (
        "cleanup did not run after a mid-run charge failure — a partial mutation was left "
        "behind (the sys.exit-before-cleanup bug)."
    )
    assert exc.value.code != 0


def _wire(monkeypatch, *, payment_ids_fn, charge_fn=None, argv):
    """Common stubs: no network, no database. Each test supplies the census/charge behaviour."""
    monkeypatch.setattr(repro, "api", lambda *a, **k: (200, {"ok": True}))
    monkeypatch.setattr(repro, "login", lambda user, password: "tok")
    monkeypatch.setattr(repro, "balance_of", lambda loan_id: 24800.00)
    monkeypatch.setattr(repro, "payment_ids", payment_ids_fn)
    if charge_fn is not None:
        monkeypatch.setattr(repro, "charge", charge_fn)
    monkeypatch.setattr(sys, "argv", argv)


def test_default_cleanup_refuses_entirely_when_a_foreign_row_is_detected(monkeypatch):
    """A concurrent, unrelated payment on the same loan must make cleanup refuse ENTIRELY.

    The bug (this fix): the default finally deleted our rows but skipped only the balance
    restore when a foreign write was seen. That left this run's debit in the shared balance
    with the rows behind it gone — a balance reduced by rows that no longer exist. The fix
    refuses cleanup entirely on any foreign row: neither the rows nor the balance is touched,
    so the two stay consistent for a human to reconcile.
    """
    created_by_charge: set[int] = set()
    state = {"calls": 0}
    lock = threading.Lock()
    ids = iter([10, 11, 12, 13, 14])

    def fake_payment_ids(loan_id):
        with lock:
            state["calls"] += 1
            base = {1, 2, 3} | set(created_by_charge)
            # A concurrent, unrelated payment lands after the opening census (call 1).
            if state["calls"] > 1:
                base |= {99}
            return base

    def fake_charge(token, loan_id, amount):
        with lock:
            pid = next(ids)
            created_by_charge.add(pid)
        return 201, {"payment_id": pid, "status": "captured"}

    calls = {"cleanup": False, "refused": None}
    monkeypatch.setattr(
        repro, "cleanup", lambda *a, **k: calls.__setitem__("cleanup", True)
    )
    monkeypatch.setattr(
        repro,
        "refuse_cleanup",
        lambda loan_id, our_ids, foreign, opening: calls.__setitem__(
            "refused", (set(our_ids), set(foreign))
        ),
    )
    _wire(
        monkeypatch,
        payment_ids_fn=fake_payment_ids,
        charge_fn=fake_charge,
        argv=["repro_double_charge.py", "--parallel", "3"],
    )

    repro.main()

    assert calls["cleanup"] is False, (
        "cleanup ran despite a foreign row — deleting our rows while a foreign write is "
        "present orphans this run's debit in the shared balance."
    )
    assert calls["refused"] is not None, "a foreign row must trigger refuse_cleanup"
    our_ids, foreign = calls["refused"]
    assert foreign == {99} and our_ids == {10, 11, 12, 13, 14}


def test_default_cleanup_runs_when_no_foreign_row(monkeypatch):
    """The happy path: no concurrent write, so cleanup deletes exactly our rows and restores."""
    created_by_charge: set[int] = set()
    lock = threading.Lock()
    ids = iter([10, 11, 12, 13, 14])

    def fake_payment_ids(loan_id):
        with lock:
            return {1, 2, 3} | set(created_by_charge)

    def fake_charge(token, loan_id, amount):
        with lock:
            pid = next(ids)
            created_by_charge.add(pid)
        return 201, {"payment_id": pid, "status": "captured"}

    deleted: dict = {}
    monkeypatch.setattr(
        repro,
        "cleanup",
        lambda loan_id, del_ids, restore_to: deleted.update(ids=set(del_ids)),
    )
    _wire(
        monkeypatch,
        payment_ids_fn=fake_payment_ids,
        charge_fn=fake_charge,
        argv=["repro_double_charge.py", "--parallel", "3"],
    )

    repro.main()

    assert deleted["ids"] == {10, 11, 12, 13, 14}, (
        "cleanup must delete exactly the run's own returned payment_ids."
    )


def test_cleanup_only_refuses_when_a_payment_landed_after_the_keep_run(monkeypatch):
    """--cleanup-only must not restore a stale absolute balance over a later legitimate payment.

    The bug (this fix): --cleanup-only deleted the saved ids and reset the balance to the saved
    opening unconditionally. A payment that landed on the same loan between the --keep run and
    this cleanup was erased by that absolute restore. The fix records the --keep run's
    high-water id; any row past it means the loan moved since, and cleanup refuses.
    """
    # Loan now holds a row (500) with an id past the --keep run's high-water of 119 — a
    # legitimate payment that landed after that run.
    monkeypatch.setattr(repro, "payment_ids", lambda loan_id: {118, 119, 500})

    calls = {"cleanup": False, "refused": None}
    monkeypatch.setattr(
        repro, "cleanup", lambda *a, **k: calls.__setitem__("cleanup", True)
    )
    monkeypatch.setattr(
        repro,
        "refuse_cleanup",
        lambda loan_id, our_ids, foreign, opening: calls.__setitem__(
            "refused", set(foreign)
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "repro_double_charge.py",
            "--cleanup-only",
            "--loan-id",
            "4471",
            "--ids",
            "118,119",
            "--restore-balance",
            "24800.00",
            "--high-water",
            "119",
        ],
    )

    rc = repro.main()

    assert calls["cleanup"] is False, (
        "cleanup ran and restored a stale absolute balance over a payment (id 500) that "
        "landed after the --keep run."
    )
    assert calls["refused"] == {500}
    assert rc == 1


def test_cleanup_only_runs_when_the_loan_has_not_moved(monkeypatch):
    """--cleanup-only happy path: no row past the high-water, so it deletes and restores."""
    monkeypatch.setattr(repro, "payment_ids", lambda loan_id: {118, 119})

    deleted: dict = {}
    monkeypatch.setattr(
        repro,
        "cleanup",
        lambda loan_id, del_ids, restore_to: deleted.update(
            ids=set(del_ids), restore_to=restore_to
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "repro_double_charge.py",
            "--cleanup-only",
            "--loan-id",
            "4471",
            "--ids",
            "118,119",
            "--restore-balance",
            "24800.00",
            "--high-water",
            "119",
        ],
    )

    rc = repro.main()

    assert deleted["ids"] == {118, 119}
    assert deleted["restore_to"] == 24800.00
    assert rc == 0
