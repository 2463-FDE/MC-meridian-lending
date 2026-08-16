"""The break report — spec `docs/spec-observability-week7.md` §D3.

`python -m app.reconcile --from YYYY-MM-DD --to YYYY-MM-DD`: one JSON document on
stdout so a piped run is parseable, a human summary on stderr so a terminal run is
readable, and the D2 exit codes unchanged (0 clean / 1 breaks / 2 abort).

The figures are pinned literals from the spec's independent solve, same as
`test_reconciliation.py`. Never regenerate them from this code.
"""

import datetime as dt
import json

import pytest

from app import reconcile, reconciliation
from tests.test_reconciliation import SAMPLE_SETTLEMENT, SEEDED_LEDGER, Recorder


@pytest.fixture(autouse=True)
def duplicate_window(monkeypatch):
    """Patch the module constant, not the environment.

    `reconciliation` reads `DUPLICATE_SUSPECT_WINDOW_SECONDS` from config at import
    time, so `setenv` after import changes nothing and every run here aborted with
    exit 2 over an unset bound. Same shape as `test_reconciliation.py`'s `ledger`
    fixture.
    """
    monkeypatch.setattr(reconciliation, "DUPLICATE_SUSPECT_WINDOW_SECONDS", "300")
    # D4's threshold, at the client's real value: the CLI is the surface the alert is
    # delivered through, so these tests should see what an operator sees.
    monkeypatch.setattr(reconciliation, "RECONCILIATION_ALERT_THRESHOLD_MINOR", "500")


@pytest.fixture
def sample(monkeypatch):
    """The seeded June ledger against the real `db/settlement.csv`."""

    def _install(rows=SEEDED_LEDGER, settlement=SAMPLE_SETTLEMENT):
        monkeypatch.setattr(reconciliation.db, "query", Recorder(rows))
        monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE", str(settlement))

    return _install


def run(capsys, argv=()):
    """Run the entrypoint and return (exit_code, parsed stdout JSON or None, stderr)."""
    code = reconcile.main(list(argv))
    captured = capsys.readouterr()
    document = json.loads(captured.out) if captured.out.strip() else None
    return code, document, captured.err


# --- D3(a)/(b) invocation and output shape ---------------------------------------


def test_sample_run_exits_1_and_writes_one_json_document_to_stdout(sample, capsys):
    """D3(a)+(b) / acceptance criterion 4, through the entrypoint."""
    sample()

    code, document, stderr = run(capsys)

    assert code == reconciliation.EXIT_BREAKS
    assert document is not None
    assert stderr.strip(), "a terminal run must get a human summary on stderr"


def test_stdout_is_json_and_nothing_else_so_a_piped_run_parses(sample, capsys):
    """D3(b) — the human summary goes to stderr, never into the parseable stream."""
    sample()

    reconcile.main([])
    captured = capsys.readouterr()

    json.loads(captured.out)  # raises if the summary leaked into stdout
    assert "does not depend" not in captured.out


def test_report_carries_the_window_and_the_tolerance(sample, capsys):
    sample()

    _, document, _ = run(capsys, ["--from", "2026-06-01", "--to", "2026-06-07"])

    assert document["window"]["from"] == "2026-06-01"
    assert document["window"]["to"] == "2026-06-07"
    assert document["window"]["tolerance_days"] == reconciliation.MATCH_TOLERANCE_DAYS


def test_report_carries_per_side_row_counts_and_minor_unit_totals(sample, capsys):
    """D3(b) — "per-side row counts and minor-unit totals".

    Seven ledger rows totalling 228535; twelve settlement rows netting 317417 across
    eleven captures and one refund. Their difference is the net variance.
    """
    sample()

    _, document, _ = run(capsys)

    assert document["ledger"] == {"rows": 7, "total_minor": 228535}
    assert document["settlement"] == {
        "rows": 12,
        "captures_minor": 360635,
        "refunds_minor": 43218,
        "net_minor": 317417,
    }
    assert (
        document["ledger"]["total_minor"] - document["settlement"]["net_minor"]
        == document["figures"]["net_variance_minor"]["value"]
    )


def test_every_break_carries_class_loan_amount_and_date(sample, capsys):
    """D3(b) — and the `processor_ref` wherever the settlement side is known."""
    sample()

    _, document, _ = run(capsys)

    assert len(document["breaks"]) == 5
    for item in document["breaks"]:
        assert item["class"] in (
            reconciliation.MISSING_IN_LEDGER,
            reconciliation.MISSING_IN_SETTLEMENT,
            reconciliation.REFUND_UNREPRESENTED,
            reconciliation.AMOUNT_MISMATCH,
        )
        assert isinstance(item["loan_id"], int)
        assert isinstance(item["amount_minor"], int)
        dt.date.fromisoformat(item["date"])

    settlement_side = [
        b
        for b in document["breaks"]
        if b["class"]
        in (reconciliation.MISSING_IN_LEDGER, reconciliation.REFUND_UNREPRESENTED)
    ]
    assert settlement_side, "the sample's breaks are all on the settlement side"
    for item in settlement_side:
        assert item["processor_ref"].startswith("PR-")


def test_duplicate_suspects_are_reported_separately_from_the_breaks(sample, capsys):
    """D3(b) + D2(e) — a signal, not a variance, so it is never inside `breaks`."""
    sample()

    _, document, _ = run(capsys)

    assert len(document["duplicate_suspects"]) == 1
    duplicate = document["duplicate_suspects"][0]
    assert duplicate["loan_id"] == 5582
    assert duplicate["amount_minor"] == 41050
    assert duplicate["gap_seconds"] == 2
    assert all(
        b["class"] != reconciliation.DUPLICATE_SUSPECT for b in document["breaks"]
    )
    assert duplicate["amount_minor"] not in (
        document["figures"]["net_variance_minor"]["value"],
        document["figures"]["gross_break_value_minor"]["value"],
    )


def test_a_duplicate_suspect_names_both_payment_ids_it_pairs(sample, capsys):
    """Review finding — a count without row evidence cannot drive remediation.

    The two ids ARE the remediation instruction: they are the rows an operator voids
    or refunds. `DuplicateSuspect` is not a `Break` — it has no `break_class` and no
    single `payment_id` — so rendering it through `_break_json` raised
    `AttributeError: 'DuplicateSuspect' object has no attribute 'break_class'` on any
    run that found one, and the seeded sample always finds loan 5582's pair. Both the
    CLI and `peek` produced a traceback instead of a document.

    The stderr summary carries the ids too: an operator reading the terminal must not
    have to re-run piped through `jq` to learn which two rows to act on.
    """
    sample()

    _, document, stderr = run(capsys)

    duplicate = document["duplicate_suspects"][0]
    assert duplicate["class"] == reconciliation.DUPLICATE_SUSPECT
    assert duplicate["first_payment_id"] == 2
    assert duplicate["second_payment_id"] == 3
    assert duplicate["date"] == "2026-06-01"
    assert "payments 2,3" in stderr


# --- D3(c) the three figures -----------------------------------------------------


def test_all_three_figures_are_reported_adjacent_and_labelled(sample, capsys):
    """D3(c) / acceptance criterion 5.

    Reporting only the net is the failure that produced "month-end is a little
    noisy": −88882 against 175318 means the netting hides roughly half the error.
    Reporting the gross without saying it moves with the tolerance would be the same
    mistake again, so each figure carries what it depends on.
    """
    sample()

    _, document, _ = run(capsys)

    figures = document["figures"]
    assert figures["net_variance_minor"] == {
        "value": -88882,
        "depends_on_matching_tolerance": False,
    }
    assert figures["per_loan_absolute_variance_minor"] == {
        "value": 175318,
        "depends_on_matching_tolerance": False,
    }
    assert figures["gross_break_value_minor"] == {
        "value": 175318,
        "depends_on_matching_tolerance": True,
    }


def test_the_human_summary_states_all_three_figures_with_their_dependence(
    sample, capsys
):
    """D3(c) — a reader of the terminal output must not have to infer this.

    The net and the gross coincide at ±1 day on this sample. That is a coincidence of
    the data, not a property, and the summary must not let a reader conclude otherwise.
    """
    sample()

    _, _, stderr = run(capsys)

    assert "-88882" in stderr
    assert "175318" in stderr
    assert stderr.count("175318") >= 2, (
        "per-loan absolute and gross are both 175318 here"
    )
    assert "does not depend on the matching tolerance" in stderr
    assert "depends on the matching tolerance" in stderr


def test_gross_moves_with_the_tolerance_while_the_other_two_do_not(sample, capsys):
    """D3(c) — the labels are true, not decorative."""
    sample()
    _, wide, _ = run(capsys, ["--tolerance-days", "1"])
    sample()
    _, tight, _ = run(capsys, ["--tolerance-days", "0"])

    assert (
        wide["figures"]["gross_break_value_minor"]["value"]
        != tight["figures"]["gross_break_value_minor"]["value"]
    )
    for stable in ("net_variance_minor", "per_loan_absolute_variance_minor"):
        assert wide["figures"][stable]["value"] == tight["figures"][stable]["value"]


# --- Exit codes and fail-closed --------------------------------------------------


def test_clean_run_exits_0(sample, capsys, tmp_path):
    settlement = tmp_path / "settlement.csv"
    settlement.write_text(
        "settlement_date,processor_ref,loan_id,amount,type\n"
        "2026-06-01,PR-1,4471,250.00,capture\n"
    )
    sample(
        rows=[
            {
                "id": 1,
                "loan_id": 4471,
                "amount": 250.00,
                "created_at": dt.datetime(2026, 6, 1, 9, 0, 0),
            }
        ],
        settlement=settlement,
    )

    code, document, _ = run(capsys)

    assert code == reconciliation.EXIT_CLEAN
    assert document["breaks"] == []


def test_abort_exits_2_and_writes_no_json_at_all(sample, capsys, tmp_path):
    """D2(g) at the CLI boundary — the whole point of the exit-code split.

    A caller piping stdout into a dashboard must not receive a well-formed report
    from a run that read nothing. "Could not check" is never a document.
    """
    sample(settlement=tmp_path / "absent.csv")

    code, document, stderr = run(capsys)

    assert code == reconciliation.EXIT_ABORT
    assert document is None, "an abort must not emit a report"
    assert "ABORT" in stderr
    assert "absent.csv" in stderr


def test_an_unparseable_date_argument_exits_2(sample, capsys):
    """A usage error is a run that did not happen — the abort code, not a crash."""
    sample()

    code, document, stderr = run(capsys, ["--from", "06/01/2026"])

    assert code == reconciliation.EXIT_ABORT
    assert document is None
    assert "06/01/2026" in stderr


def test_an_inverted_window_exits_2(sample, capsys):
    sample()

    code, document, _ = run(capsys, ["--from", "2026-06-07", "--to", "2026-06-01"])

    assert code == reconciliation.EXIT_ABORT
    assert document is None


def test_an_explicit_window_narrows_the_report(sample, capsys):
    sample()

    _, document, _ = run(capsys, ["--from", "2026-06-01", "--to", "2026-06-03"])

    assert document["window"]["to"] == "2026-06-03"
    assert document["settlement"]["rows"] == 7


# --- D3(d) peek runs the same code path ------------------------------------------


def test_peek_returns_the_same_document_the_cli_writes(sample, capsys, monkeypatch):
    """D3(d) — one comparison, not two.

    Keeping a second, weaker comparison beside the real one reproduces the drift the
    fee-schedule loader was built to end, which is why `ledger_total`/
    `settlement_total` were deleted rather than left in place.
    """
    from fastapi.testclient import TestClient

    from app import config
    from app.main import app

    sample()
    _, from_cli, _ = run(capsys)

    sample()
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    resp = TestClient(app).get(
        "/reconciliation/peek", headers={"X-Internal-Service": "sekret"}
    )

    assert resp.status_code == 200
    assert resp.json() == from_cli
