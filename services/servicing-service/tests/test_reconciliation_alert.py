"""D4 — one alert on the reconciliation outcome.

Spec `docs/spec-observability-week7.md` §D4; threshold set by the client on 2026-08-14 at
$5.00 aggregate per close, so 500 minor units, from configuration with no default.

The alert measures PER-LOAN ABSOLUTE variance, which is the choice most likely to be
re-argued later, so it is pinned here three ways: not the net (it cancels opposite-signed
breaks — on the sample −88882 against 175318), not the gross break value (it moves with
the matching tolerance, 175318 at ±1 day against 257418 at ±0), and not a count (breaks
exist in the sample today, so a count-based alert fires every close from day one).

Money figures are pinned literals; never regenerate them from the code under test.
"""

import datetime as dt

import pytest

from app import config, reconciliation
from tests.test_reconciliation import (  # noqa: F401
    SAMPLE_SETTLEMENT,
    ledger,
    write_settlement,
)

CLIENT_THRESHOLD = "500"


@pytest.fixture
def threshold(monkeypatch):
    """Set the alert threshold the way a deploy does — the module constant."""

    def _install(value=CLIENT_THRESHOLD):
        monkeypatch.setattr(
            reconciliation, "RECONCILIATION_ALERT_THRESHOLD_MINOR", value
        )
        monkeypatch.setattr(config, "RECONCILIATION_ALERT_THRESHOLD_MINOR", value)

    return _install


# --- Fail closed ------------------------------------------------------------------


def test_an_unset_threshold_aborts_rather_than_alerting_on_nothing(ledger, tmp_path):  # noqa: F811
    """No default. A guessed threshold alerts on everything or on nothing, and both
    read as a working control from the outside."""
    ledger()
    reconciliation.RECONCILIATION_ALERT_THRESHOLD_MINOR = ""

    with pytest.raises(reconciliation.ReconciliationAbort) as excinfo:
        reconciliation.reconcile(settlement_path=str(SAMPLE_SETTLEMENT))

    assert "RECONCILIATION_ALERT_THRESHOLD_MINOR" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["abc", "5.00", "-100", "0", " ", "1_000", "+500"])
def test_an_unusable_threshold_aborts(ledger, tmp_path, threshold, bad):  # noqa: F811
    """Zero is refused too: the one way variance is nonzero with no breaks is a match
    pairing across the window edge, so a zero threshold would alert on ordinary cut-off
    timing every close. An operator who wants that says 1, not a value that also reads
    as unset."""
    ledger()
    threshold(bad)

    with pytest.raises(reconciliation.ReconciliationAbort):
        reconciliation.reconcile(settlement_path=str(SAMPLE_SETTLEMENT))


def test_health_reports_the_threshold_as_a_required_setting(monkeypatch):
    """Unguarded, a deploy passes /health and the month-end run aborts — the operator
    learns the control was never configured at the moment they need its answer."""
    monkeypatch.setattr(config, "RECONCILIATION_ALERT_THRESHOLD_MINOR", "")

    assert "RECONCILIATION_ALERT_THRESHOLD_MINOR" in config.missing_required_secrets()

    monkeypatch.setattr(config, "RECONCILIATION_ALERT_THRESHOLD_MINOR", "500")

    assert (
        "RECONCILIATION_ALERT_THRESHOLD_MINOR" not in config.missing_required_secrets()
    )


@pytest.mark.parametrize("bad", ["1_000", "+500"])
def test_health_rejects_what_the_runtime_parser_rejects(monkeypatch, bad):
    """Readiness and the runtime parser (reconciliation._alert_threshold_minor, via
    the shared config._PLAIN_INTEGER) must agree on what a valid value is. Before this
    fix, readiness took bare int() (accepts "1_000" and "+500") while the runtime took
    a plain-digit regex (rejects both) — a deploy could pass /health with a threshold
    the month-end run then refused to use, aborting with no report while readiness
    still says "configured"."""
    monkeypatch.setattr(config, "RECONCILIATION_ALERT_THRESHOLD_MINOR", bad)

    assert config.reconciliation_alert_threshold_configured() is False
    assert "RECONCILIATION_ALERT_THRESHOLD_MINOR" in config.missing_required_secrets()


# --- The alert itself -------------------------------------------------------------


def test_the_sample_breaches_the_clients_threshold_on_day_one(ledger, threshold):  # noqa: F811
    """The consequence to state out loud on Monday, pinned so it cannot drift.

    At 500 minor the seeded sample alerts immediately: per-loan absolute variance is
    175318, roughly 350x the threshold. That is not a mis-set threshold — it is loan
    4471's open exception showing up in the alert exactly as intended.
    """
    ledger()
    threshold()

    result = reconciliation.reconcile(settlement_path=str(SAMPLE_SETTLEMENT))

    assert result.per_loan_absolute_minor == 175318
    assert result.alert_threshold_minor == 500
    assert result.alert_triggered is True
    assert result.exit_code == reconciliation.EXIT_BREAKS


def test_a_variance_exactly_at_the_threshold_does_not_alert(
    ledger, threshold, tmp_path
):  # noqa: F811
    """ "Exceeds" is strictly greater: a variance exactly at the threshold is the largest
    difference the client called acceptable, not the smallest one they wanted to hear
    about."""
    ledger(
        [
            {
                "id": 1,
                "loan_id": 4471,
                "amount": 5.00,
                "created_at": dt.datetime(2026, 6, 1, 9, 0, 0),
            }
        ]
    )
    threshold()
    path = write_settlement(tmp_path, ["2026-06-01,PR-1,4471,10.00,capture"])

    result = reconciliation.reconcile(settlement_path=path)

    assert result.per_loan_absolute_minor == 500
    assert result.alert_triggered is False


def test_one_minor_unit_over_the_threshold_alerts(ledger, threshold, tmp_path):  # noqa: F811
    """The other side of the same boundary."""
    ledger(
        [
            {
                "id": 1,
                "loan_id": 4471,
                "amount": 4.99,
                "created_at": dt.datetime(2026, 6, 1, 9, 0, 0),
            }
        ]
    )
    threshold()
    path = write_settlement(tmp_path, ["2026-06-01,PR-1,4471,10.00,capture"])

    result = reconciliation.reconcile(settlement_path=path)

    assert result.per_loan_absolute_minor == 501
    assert result.alert_triggered is True


def test_a_clean_window_does_not_alert(ledger, threshold, tmp_path):  # noqa: F811
    """Zero variance, no breaks, no alert, exit 0 — the shape a good close has."""
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
    threshold()
    path = write_settlement(tmp_path, ["2026-06-01,PR-100231,4471,250.00,capture"])

    result = reconciliation.reconcile(settlement_path=path)

    assert result.alert_triggered is False
    assert result.exit_code == reconciliation.EXIT_CLEAN


def test_a_duplicate_suspect_does_not_feed_the_alert(ledger, threshold, tmp_path):  # noqa: F811
    """D2(e) — a duplicate carries no variance, and adding it would count money already
    counted on the side it landed on. It still forces a non-clean exit on its own."""
    ledger(
        [
            {
                "id": 1,
                "loan_id": 5582,
                "amount": 410.50,
                "created_at": dt.datetime(2026, 6, 1, 9, 31, 4),
            },
            {
                "id": 2,
                "loan_id": 5582,
                "amount": 410.50,
                "created_at": dt.datetime(2026, 6, 1, 9, 31, 6),
            },
        ]
    )
    threshold()
    path = write_settlement(
        tmp_path,
        [
            "2026-06-01,PR-1,5582,410.50,capture",
            "2026-06-01,PR-2,5582,410.50,capture",
        ],
    )

    result = reconciliation.reconcile(settlement_path=path)

    assert len(result.duplicates) == 1
    assert result.per_loan_absolute_minor == 0
    assert result.alert_triggered is False
    assert result.exit_code == reconciliation.EXIT_BREAKS


# --- What the threshold must NOT do -----------------------------------------------


def test_a_sub_threshold_break_still_appears_in_the_report(ledger, threshold, tmp_path):  # noqa: F811
    """The client asked for this in the same answer that set the threshold:
    "individual unmatched transactions should still appear in the exception output
    regardless of amount".

    The threshold gates the ALERT, never the report. Without this test a later
    "optimisation" could start filtering breaks by the alert threshold and the exception
    output would quietly stop listing the small ones — the exact failure the report
    exists to prevent, reintroduced as a performance tweak.
    """
    ledger(
        [
            {
                "id": 1,
                "loan_id": 4471,
                "amount": 1.00,
                "created_at": dt.datetime(2026, 6, 1, 9, 0, 0),
            }
        ]
    )
    threshold()
    path = write_settlement(
        tmp_path,
        [
            "2026-06-01,PR-1,4471,1.00,capture",
            "2026-06-01,PR-2,6011,0.99,capture",
        ],
    )

    result = reconciliation.reconcile(settlement_path=path)

    assert result.alert_triggered is False, "99 minor units is under the 500 threshold"
    missing = [
        b for b in result.breaks if b.break_class == reconciliation.MISSING_IN_LEDGER
    ]
    assert len(missing) == 1
    assert missing[0].amount_minor == 99
    assert missing[0].processor_ref == "PR-2"

    document = reconciliation.build_report(result)
    assert len(document["breaks"]) == 1
    assert document["breaks"][0]["amount_minor"] == 99
    assert document["alert"]["triggered"] is False


def test_the_report_states_the_threshold_it_measured_against(ledger, threshold):  # noqa: F811
    """A bare boolean makes a reader look up a deploy's environment to interpret it."""
    ledger()
    threshold()

    document = reconciliation.build_report(
        reconciliation.reconcile(settlement_path=str(SAMPLE_SETTLEMENT))
    )

    assert document["alert"] == {
        "triggered": True,
        "threshold_minor": 500,
        "measured": "per_loan_absolute_variance_minor",
        "value": 175318,
    }


def test_the_alert_does_not_read_the_net_or_the_gross(ledger, threshold, tmp_path):  # noqa: F811
    """Two opposite-signed breaks on different loans: the net cancels to zero while the
    per-loan absolute is 20000. Alerting on the net would report this close as quiet.
    """
    ledger(
        [
            {
                "id": 1,
                "loan_id": 4471,
                "amount": 100.00,
                "created_at": dt.datetime(2026, 6, 1, 9, 0, 0),
            }
        ]
    )
    threshold()
    path = write_settlement(tmp_path, ["2026-06-01,PR-1,6011,100.00,capture"])

    result = reconciliation.reconcile(settlement_path=path)

    assert result.net_variance_minor == 0
    assert result.per_loan_absolute_minor == 20000
    assert result.alert_triggered is True
