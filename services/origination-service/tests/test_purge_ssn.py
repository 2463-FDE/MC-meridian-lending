"""D33 purge mechanism (docs/debt-log.md, app/purge_ssn.py) -- inert by construction.

The whole point of this file is that the mechanism ships without ever running against
real data: a dry run (the default) must never open a write transaction, and --execute
alone must not purge anything unless SSN_PURGE_ENABLED is also set. DB is stubbed --
no live Postgres in unit tests.
"""

from contextlib import contextmanager

import pytest

from app import config, purge_ssn


@pytest.fixture(autouse=True)
def _no_purge_enabled_by_default(monkeypatch):
    # Every test starts from the shipped-safe default; tests that need it on set it
    # explicitly, so a test order change can't leak an enabled purge into another test.
    monkeypatch.setattr(config, "SSN_PURGE_ENABLED", False)


def _fake_count_query(n):
    def _q(sql, params=None):
        assert "count(*)" in sql
        assert "applicants" in sql
        return [{"n": n}]

    return _q


def test_dry_run_never_opens_a_transaction(monkeypatch):
    monkeypatch.setattr(purge_ssn.db, "query", _fake_count_query(3))

    def _must_not_open(*a, **k):
        raise AssertionError("dry run must never open a write transaction")

    monkeypatch.setattr(purge_ssn.db, "transaction", _must_not_open)
    result = purge_ssn.run(window_days=30, execute=False)
    assert result == {
        "mode": "dry_run",
        "window_days": 30,
        "eligible": 3,
        "purged": 0,
    }


def test_execute_without_enabled_still_dry_runs(monkeypatch):
    # The second, independent gate: --execute alone (config.SSN_PURGE_ENABLED unset)
    # must not purge, and must say why rather than silently no-op.
    monkeypatch.setattr(purge_ssn.db, "query", _fake_count_query(5))

    def _must_not_open(*a, **k):
        raise AssertionError("execute=True with the config gate off must not purge")

    monkeypatch.setattr(purge_ssn.db, "transaction", _must_not_open)
    result = purge_ssn.run(window_days=30, execute=True)
    assert result["mode"] == "dry_run"
    assert result["purged"] == 0
    assert "SSN_PURGE_ENABLED" in result["reason"]


class _FakeCursor:
    def __init__(self):
        self.executed = []
        self.rowcount = 7

    def execute(self, sql, params=None):
        self.executed.append((sql, params))


def test_execute_with_both_gates_open_purges_and_backfills_last4(monkeypatch):
    monkeypatch.setattr(config, "SSN_PURGE_ENABLED", True)
    cur = _FakeCursor()

    @contextmanager
    def _txn():
        yield cur

    monkeypatch.setattr(purge_ssn.db, "transaction", _txn)
    result = purge_ssn.run(window_days=45, execute=True)
    assert result == {
        "mode": "executed",
        "window_days": 45,
        "eligible": None,
        "purged": 7,
    }
    sql, params = cur.executed[0]
    assert "SET ssn_last4 = COALESCE(ssn_last4, RIGHT(ssn, 4))" in sql
    assert "ssn = NULL" in sql
    assert params == (45,)


def test_configured_window_days_requires_explicit_value(monkeypatch):
    monkeypatch.setattr(config, "SSN_PURGE_WINDOW_DAYS", "")
    with pytest.raises(purge_ssn.PurgeAbort, match="not set"):
        purge_ssn._configured_window_days()


@pytest.mark.parametrize("raw", ["not-a-number", "0", "-5"])
def test_configured_window_days_rejects_invalid_values(monkeypatch, raw):
    monkeypatch.setattr(config, "SSN_PURGE_WINDOW_DAYS", raw)
    with pytest.raises(purge_ssn.PurgeAbort):
        purge_ssn._configured_window_days()


def test_main_aborts_when_window_not_configured(monkeypatch, capsys):
    monkeypatch.setattr(config, "SSN_PURGE_WINDOW_DAYS", "")
    exit_code = purge_ssn.main([])
    assert exit_code == purge_ssn.EXIT_ABORT
    captured = capsys.readouterr()
    assert captured.out == ""  # no document on abort, mirrors app/reconcile.py
    assert "ABORT" in captured.err


def test_main_dry_run_reports_via_cli(monkeypatch, capsys):
    monkeypatch.setattr(purge_ssn.db, "query", _fake_count_query(2))
    exit_code = purge_ssn.main(["--window-days", "30"])
    assert exit_code == purge_ssn.EXIT_OK
    captured = capsys.readouterr()
    assert '"mode": "dry_run"' in captured.out
    assert '"eligible": 2' in captured.out
