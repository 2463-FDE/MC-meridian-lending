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
    charge_codes = iter([(201, {"id": 99}), (500, "processor exploded")])
    monkeypatch.setattr(
        repro, "charge", lambda token, loan_id, amount: next(charge_codes)
    )

    def fake_cleanup(loan_id, ids, restore_to):
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
