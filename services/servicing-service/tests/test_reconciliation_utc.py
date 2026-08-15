"""The reconciliation cut-off is the settled date in UTC — client answer 2026-08-14.

`payments.created_at` is TIMESTAMPTZ (`db/init/001_schema.sql:138`) and matching compares
`created_at.date()` against the settlement date. psycopg2 returns an aware datetime in the
SESSION's timezone, so that `.date()` is a UTC date only if the session is UTC — and
nothing pinned it. On a UTC+2 database a capture at 23:30 UTC reads as the next calendar
day and lands on the far side of the window.

It does NOT show up as a pair of breaks, which is what makes it worth its own file. The
±1 day matching tolerance absorbs the shift, so the row still matches its capture and the
break list stays empty. The TOTALS are cut to the narrow window without the tolerance
(D2(a)), so the ledger side loses the row while the settlement side keeps its capture:
net variance and per-loan absolute both read -25000 for a payment captured, settled and
credited exactly once, and no break in the report contradicts them. A wrong figure that
looks reconciled, rather than a finding an operator can chase.

Two guards, because the defect has two halves. The session pin stops the database handing
back a shifted date at all; the row-boundary conversion makes every downstream `.date()`
call a UTC date regardless of where the rows came from.

Money figures are pinned literals; never regenerate them from the code under test.
"""

import datetime as dt

import pytest

from app import db, reconciliation
from tests.test_reconciliation import ledger, write_settlement  # noqa: F401

# UTC+2. A capture at 23:30 UTC is 01:30 the NEXT day here — the shift the client's
# answer forbids, and the one an unpinned session silently applies.
EAST = dt.timezone(dt.timedelta(hours=2))


def test_the_session_timezone_is_pinned_to_utc(monkeypatch):
    """The root cause: `psycopg2.connect(DATABASE_URL)` carried no options.

    Asserting the option rather than the resulting session because the test suite has no
    live Postgres. A wrong option here is caught by the behavioural tests below only if
    the database happens to be non-UTC, which is exactly the deployment this fix exists
    for and never the one CI runs on.
    """
    seen = {}

    class FakeConn:
        closed = False
        autocommit = False

    def fake_connect(dsn, **kwargs):
        seen["dsn"] = dsn
        seen["kwargs"] = kwargs
        return FakeConn()

    monkeypatch.setattr(db.psycopg2, "connect", fake_connect)
    monkeypatch.setattr(db, "_conn", None)

    db.get_conn()

    options = seen["kwargs"].get("options", "")
    assert "timezone=UTC" in options.replace(" ", "").replace("-c", "-c "), (
        f"the session timezone is not pinned to UTC: {seen['kwargs']!r}"
    )


def test_a_capture_near_midnight_classifies_on_its_utc_date(ledger, tmp_path):  # noqa: F811
    """23:30 UTC on 06-02, delivered as 01:30 on 06-03 in a UTC+2 session.

    The settlement file settles it on 06-02. Read as 06-03 the row falls outside a
    06-02..06-02 window entirely and the capture is orphaned; read in UTC it matches and
    the run is clean.
    """
    ledger(
        [
            {
                "id": 1,
                "loan_id": 4471,
                "amount": 250.00,
                "created_at": dt.datetime(2026, 6, 3, 1, 30, 0, tzinfo=EAST),
            }
        ]
    )
    path = write_settlement(tmp_path, ["2026-06-02,PR-100231,4471,250.00,capture"])

    result = reconciliation.reconcile(
        from_date=dt.date(2026, 6, 2),
        to_date=dt.date(2026, 6, 2),
        settlement_path=path,
    )

    assert result.breaks == []
    assert result.matched_count == 1
    assert result.net_variance_minor == 0
    assert result.exit_code == reconciliation.EXIT_CLEAN


def test_a_shifted_row_still_counts_toward_the_windows_totals(ledger, tmp_path):  # noqa: F811
    """Where the shift actually bites: the variance figures, not the break list.

    The ±1 day matching tolerance absorbs a one-day shift, so `breaks` stays empty and
    the run looks clean. The TOTALS do not use the tolerance — they are cut to the narrow
    window on purpose (D2(a)), so a row read as 06-03 drops out of the ledger side while
    its capture stays on the settlement side. Net variance reads -25000 for a payment
    that was captured, settled and credited exactly once, and per-loan absolute agrees
    with it, so nothing in the report contradicts the wrong number.

    This is the more dangerous half of the defect: a break list an operator can
    investigate versus a variance figure that is simply wrong and looks reconciled.
    """
    ledger(
        [
            {
                "id": 1,
                "loan_id": 4471,
                "amount": 250.00,
                "created_at": dt.datetime(2026, 6, 3, 1, 30, 0, tzinfo=EAST),
            }
        ]
    )
    path = write_settlement(tmp_path, ["2026-06-02,PR-100231,4471,250.00,capture"])

    result = reconciliation.reconcile(
        from_date=dt.date(2026, 6, 2),
        to_date=dt.date(2026, 6, 2),
        settlement_path=path,
    )

    assert result.net_variance_minor == 0
    assert result.per_loan_absolute_minor == 0
    assert result.gross_break_minor == 0


def test_a_naive_timestamp_is_left_alone(ledger, tmp_path):  # noqa: F811
    """No offset to convert from, so converting would be inventing one.

    Every other test in the suite passes naive datetimes; this pins that the conversion
    is a no-op for them rather than an implicit localisation.
    """
    ledger(
        [
            {
                "id": 1,
                "loan_id": 4471,
                "amount": 250.00,
                "created_at": dt.datetime(2026, 6, 1, 9, 14, 11),
            }
        ]
    )
    path = write_settlement(tmp_path, ["2026-06-01,PR-100231,4471,250.00,capture"])

    result = reconciliation.reconcile(settlement_path=path)

    assert result.breaks == []
    assert result.exit_code == reconciliation.EXIT_CLEAN


@pytest.mark.parametrize(
    "offset_hours,captured_at",
    [
        (2, dt.datetime(2026, 6, 3, 1, 30, 0)),  # 23:30 UTC 06-02 seen as 06-03
        (-5, dt.datetime(2026, 6, 1, 20, 30, 0)),  # 01:30 UTC 06-02 seen as 06-01
    ],
)
def test_the_boundary_holds_on_both_sides_of_utc(
    ledger,  # noqa: F811
    tmp_path,
    offset_hours,
    captured_at,
):
    """A session ahead of UTC pushes the date forward, behind it pulls the date back.

    Both shift a row across a one-day window boundary. Asserting the variance figures as
    well as the break list, because matching's ±1 day tolerance hides the shift while the
    narrow-window totals do not.
    """
    zone = dt.timezone(dt.timedelta(hours=offset_hours))
    ledger(
        [
            {
                "id": 1,
                "loan_id": 4471,
                "amount": 250.00,
                "created_at": captured_at.replace(tzinfo=zone),
            }
        ]
    )
    path = write_settlement(tmp_path, ["2026-06-02,PR-100231,4471,250.00,capture"])

    result = reconciliation.reconcile(
        from_date=dt.date(2026, 6, 2),
        to_date=dt.date(2026, 6, 2),
        settlement_path=path,
    )

    assert result.breaks == []
    assert result.matched_count == 1
    assert result.net_variance_minor == 0
    assert result.per_loan_absolute_minor == 0
