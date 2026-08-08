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
import subprocess
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


def test_keep_refuses_and_prints_no_cleanup_command_when_foreign_row_present(
    monkeypatch, capsys
):
    """--keep must not hand out a runnable --cleanup-only command when a foreign row is present.

    The bug (this fix): under --keep, a foreign row only triggered a printed note — the script
    still printed a runnable --cleanup-only command whose high-water was computed from
    current_ids, which INCLUDES the foreign row. --cleanup-only only refuses rows with
    id > high-water, so that foreign row would never trip the check; running the printed
    command deletes this run's rows and restores the stale opening balance, silently erasing
    the foreign payment's balance movement. The fix refuses the same way the non-keep path
    does (refuse_cleanup) whenever a foreign row is seen, with or without --keep, and never
    prints a --cleanup-only invocation in that case.
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
        argv=["repro_double_charge.py", "--keep", "--parallel", "3"],
    )

    repro.main()
    out = capsys.readouterr().out

    assert calls["cleanup"] is False, (
        "cleanup must never run when a foreign row is present."
    )
    assert calls["refused"] is not None, (
        "a foreign row under --keep must trigger refuse_cleanup, not a printed cleanup note."
    )
    our_ids, foreign = calls["refused"]
    assert foreign == {99} and our_ids == {10, 11, 12, 13, 14}
    assert "--cleanup-only" not in out, (
        "a foreign row was present but the script still printed a runnable --cleanup-only "
        "command — running it would restore a stale absolute balance over the foreign "
        "payment's movement."
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


def test_concurrent_transport_failure_does_not_orphan_successful_siblings(monkeypatch):
    """A worker's transport failure (timeout/connection reset) must not stop successful
    siblings from being recorded, and must not make the finally block misclassify their rows
    as foreign and refuse cleanup.

    The bug (this fix): `list(pool.map(...))` raised whichever worker's transport exception
    first, before ANY response — including successful siblings that completed first — was
    processed into created_ids. The finally block then saw those already-inserted rows as
    (current_ids - ids_before_all) - created_ids, classified them as foreign, and refused
    cleanup on the script's own rows. The fix submits each charge individually and collects
    results via as_completed, catching a transport exception per-future so it cannot prevent
    the others from being recorded.
    """
    # The script drives Case A (2 sequential calls) THEN Case B (3 concurrent calls) through
    # one shared call counter — calls 1-2 are Case A's retries (must succeed, or Case B never
    # runs); calls 3-5 are Case B's concurrent attempts. Fail exactly one of Case B's calls
    # (call 4) regardless of which physical thread draws it.
    created_by_charge: set[int] = set()
    lock = threading.Lock()
    ids = iter([10, 11, 12, 13])
    call_count = {"n": 0}

    def fake_charge(token, loan_id, amount):
        with lock:
            call_count["n"] += 1
            n = call_count["n"]
        if n == 4:
            raise TimeoutError("simulated flaky gateway")
        with lock:
            pid = next(ids)
            created_by_charge.add(pid)
        return 201, {"payment_id": pid, "status": "captured"}

    def fake_payment_ids(loan_id):
        with lock:
            return {1, 2, 3} | set(created_by_charge)

    deleted: dict = {}
    monkeypatch.setattr(
        repro,
        "cleanup",
        lambda loan_id, del_ids, restore_to: deleted.update(ids=set(del_ids)),
    )
    calls = {"refused": None}
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

    assert calls["refused"] is None, (
        "a transport failure on one Case B worker made the finally block treat the script's "
        "own successful rows as foreign and refuse cleanup."
    )
    assert deleted.get("ids") == {10, 11, 12, 13}, (
        "the successful charges (2 from case A, 2 of case B's 3 concurrent attempts) were "
        "not all recorded into created_ids around the transport failure on the other worker."
    )


def test_cleanup_refuses_without_mutating_on_an_id_loan_mismatch(monkeypatch):
    """cleanup() must refuse ENTIRELY — deleting nothing and updating no balance — when a
    requested id is not actually attached to the target loan.

    The bug (this fix): cleanup() ran `DELETE FROM payments WHERE id IN (...)` with no
    loan_id predicate, then unconditionally restored loan_id's balance. A mistyped
    --loan-id with otherwise-valid ids could delete payment rows belonging to one loan while
    resetting a DIFFERENT loan's balance. The fix scopes the DELETE to loan_id and runs it in
    the same DO block as the balance UPDATE, raising (and rolling back) before the UPDATE if
    the deleted row count does not match the requested id count.
    """
    # Payment 118 is really on loan 4471; 119 is really on a different loan — the "mistyped
    # --loan-id with an otherwise-valid id" scenario the finding describes.
    payments_by_loan = {4471: {118}, 9999: {119}}
    mutated = {"called": False}

    def fake_psql(sql: str) -> str:
        mutated["called"] = True
        if "DELETE FROM payments" in sql:
            requested = {118, 119}
            actually_on_loan = payments_by_loan.get(4471, set()) & requested
            if len(actually_on_loan) != len(requested):
                raise subprocess.CalledProcessError(
                    1,
                    ["psql"],
                    output="",
                    stderr="cleanup mismatch: expected 2, deleted 1",
                )
            return ""
        raise AssertionError(f"unexpected SQL sent to psql(): {sql}")

    monkeypatch.setattr(repro, "psql", fake_psql)

    with pytest.raises(repro.CleanupMismatch):
        repro.cleanup(4471, {118, 119}, 24800.00)

    assert mutated["called"] is True, "the DO block must still be sent to the database"


def test_cleanup_deletes_and_restores_when_every_id_matches_the_loan(monkeypatch):
    """cleanup() happy path: every id genuinely belongs to loan_id, so it deletes and
    restores without raising."""
    sent = {"sql": None}

    def fake_psql(sql: str) -> str:
        sent["sql"] = sql
        return ""

    monkeypatch.setattr(repro, "psql", fake_psql)

    repro.cleanup(4471, {118, 119}, 24800.00)

    assert (
        "DELETE FROM payments WHERE loan_id = 4471 AND id IN (118,119)" in sent["sql"]
    )
    assert "UPDATE balances SET balance = 24800.0 WHERE loan_id = 4471" in sent["sql"]
