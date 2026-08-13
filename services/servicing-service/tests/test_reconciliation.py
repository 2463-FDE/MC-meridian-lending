"""Reading the settlement file — spec `docs/spec-observability-week7.md` §D2(b)/(g).

`settlement_total()` returned `0.0` when the file was absent: no exception, no signal,
a number reported over a file it never read. It also summed binary floats and asked
whether the result tied out, which is unsound at precisely the moment the answer
matters.

This covers the read and the parse only — the row-level comparison, the matching rule
and the break classes are the next change. What lands here is the contract everything
above it depends on: **a verifier never reports a result for a path it did not verify.**

Money figures are pinned literals; never regenerate them from the code under test.
"""

import pytest
from fastapi.testclient import TestClient

from app import config, reconciliation
from app.main import app

SETTLEMENT_HEADER = "settlement_date,processor_ref,loan_id,amount,type\n"


def write_settlement(tmp_path, rows, header=SETTLEMENT_HEADER):
    path = tmp_path / "settlement.csv"
    path.write_text(header + "".join(r if r.endswith("\n") else r + "\n" for r in rows))
    return str(path)


# --- Fail closed (D2(g)) ---------------------------------------------------------


def test_a_missing_file_aborts_instead_of_returning_zero(tmp_path, monkeypatch):
    """The fail-open this change exists to remove.

    `return 0.0` for a missing file is a check that reports a number when it verified
    nothing. In the deployed configuration the path resolves, so this was latent
    rather than active — it is still a verifier answering for work it did not do.
    """
    absent = str(tmp_path / "nope.csv")
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE", absent)

    with pytest.raises(reconciliation.ReconciliationAbort) as excinfo:
        reconciliation.settlement_total()

    assert absent in str(excinfo.value)


def test_an_empty_file_aborts(tmp_path):
    """Header only, zero data rows — nothing was compared, so there is no total."""
    path = write_settlement(tmp_path, [])

    with pytest.raises(reconciliation.ReconciliationAbort):
        reconciliation.load_settlement(path)


def test_a_missing_required_column_aborts_and_names_it(tmp_path):
    path = write_settlement(
        tmp_path,
        ["2026-06-01,PR-1,4471,capture"],
        header="settlement_date,processor_ref,loan_id,type\n",
    )

    with pytest.raises(reconciliation.ReconciliationAbort) as excinfo:
        reconciliation.load_settlement(path)

    assert "amount" in str(excinfo.value)


def test_an_unparseable_amount_aborts(tmp_path):
    path = write_settlement(tmp_path, ["2026-06-01,PR-1,4471,abc,capture"])

    with pytest.raises(reconciliation.ReconciliationAbort) as excinfo:
        reconciliation.load_settlement(path)

    assert "abc" in str(excinfo.value)


def test_an_unparseable_date_aborts(tmp_path):
    path = write_settlement(tmp_path, ["06/01/2026,PR-1,4471,250.00,capture"])

    with pytest.raises(reconciliation.ReconciliationAbort):
        reconciliation.load_settlement(path)


def test_an_unmodelled_type_aborts_rather_than_being_dropped(tmp_path):
    """A `chargeback` is money this code cannot classify.

    Skipping the row would let the job claim it compared the file while ignoring part
    of it — the same failure as reading nothing, scoped to one row.
    """
    path = write_settlement(tmp_path, ["2026-06-01,PR-1,4471,250.00,chargeback"])

    with pytest.raises(reconciliation.ReconciliationAbort) as excinfo:
        reconciliation.load_settlement(path)

    assert "chargeback" in str(excinfo.value)


def test_a_short_row_aborts(tmp_path):
    """A row with a missing value parses as None; it must not become 0 or "".‌"""
    path = write_settlement(tmp_path, ["2026-06-01,PR-1,4471,,capture"])

    with pytest.raises(reconciliation.ReconciliationAbort):
        reconciliation.load_settlement(path)


def test_the_abort_exit_code_is_distinct_from_clean_and_from_breaks(tmp_path):
    """D2(g) — "could not check" is never 0, and never 1 either.

    Mirrors `scripts/prove_test.sh`'s convention. The comparison that consumes these
    lands in the next change; the codes are fixed here because the abort is.
    """
    assert reconciliation.EXIT_CLEAN == 0
    assert reconciliation.EXIT_BREAKS == 1
    assert reconciliation.EXIT_ABORT == 2


# --- Money (D2(b)) ---------------------------------------------------------------


def test_amounts_parse_to_integer_minor_units(tmp_path):
    path = write_settlement(
        tmp_path,
        [
            "2026-06-01,PR-1,4471,250.00,capture",
            "2026-06-01,PR-2,5582,410.50,capture",
            "2026-06-01,PR-3,4471,99.99,capture",
        ],
    )

    rows = reconciliation.load_settlement(path)

    assert [r.amount_minor for r in rows] == [25000, 41050, 9999]
    for row in rows:
        assert isinstance(row.amount_minor, int) and not isinstance(
            row.amount_minor, bool
        )


def test_minor_units_tie_out_where_float_does_not(tmp_path):
    """V-MINOR — 0.10 + 0.20 against 0.30."""
    assert 0.10 + 0.20 != 0.30  # the implementation this replaces

    path = write_settlement(
        tmp_path,
        [
            "2026-06-01,PR-1,4471,0.10,capture",
            "2026-06-01,PR-2,4471,0.20,capture",
        ],
    )

    rows = reconciliation.load_settlement(path)

    assert sum(r.amount_minor for r in rows) == 30


def test_sub_cent_precision_aborts_rather_than_rounding(tmp_path):
    """V-DECIMAL-PARSE — `250.005` is not silently moved to a cent boundary.

    A verifier that quietly changes a figure is not verifying it.
    """
    path = write_settlement(tmp_path, ["2026-06-01,PR-1,4471,250.005,capture"])

    with pytest.raises(reconciliation.ReconciliationAbort) as excinfo:
        reconciliation.load_settlement(path)

    assert "250.005" in str(excinfo.value)


@pytest.mark.parametrize("amount", ["1_000.00", "+250.00", "2.5E+2", "NaN", "-5.00"])
def test_only_a_plain_decimal_literal_is_accepted(tmp_path, amount):
    """`Decimal` accepts all of these; none of them is a money literal.

    `Decimal("2.5E+2")` equals 250 and would compare equal to a real figure while
    reading nothing like one. Same posture as the disclosure figure check.
    """
    path = write_settlement(tmp_path, [f"2026-06-01,PR-1,4471,{amount},capture"])

    with pytest.raises(reconciliation.ReconciliationAbort):
        reconciliation.load_settlement(path)


def test_a_refund_is_read_as_a_refund_not_a_negative_capture(tmp_path):
    """The row type is carried, not folded into the sign at read time.

    `payments` has no direction column, so a refund cannot have a counterpart there.
    The comparison needs to say that specifically rather than net it away.
    """
    path = write_settlement(tmp_path, ["2026-06-05,PR-100299,6011,432.18,refund"])

    rows = reconciliation.load_settlement(path)

    assert len(rows) == 1
    assert rows[0].row_type == reconciliation.REFUND
    assert rows[0].amount_minor == 43218
    assert rows[0].processor_ref == "PR-100299"


def test_settlement_total_nets_refunds_against_captures(tmp_path, monkeypatch):
    """The existing helper keeps its meaning; only its failure mode changes.

    Deleting it belongs with the row-level comparison that replaces it, not here.
    """
    path = write_settlement(
        tmp_path,
        [
            "2026-06-01,PR-1,4471,250.00,capture",
            "2026-06-05,PR-2,6011,432.18,refund",
        ],
    )
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE", path)

    assert reconciliation.settlement_total() == pytest.approx(250.00 - 432.18)


# --- The peek route --------------------------------------------------------------


def test_peek_reports_an_abort_as_503_never_a_200_carrying_zeroes(
    tmp_path, monkeypatch
):
    """The fail-open at the HTTP boundary.

    Before this change the route answered 200 with `settlement_total: 0.0` for a file
    it never opened. An unverifiable comparison must not look like a successful one.
    """
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE", str(tmp_path / "absent.csv"))

    resp = TestClient(app).get(
        "/reconciliation/peek", headers={"X-Internal-Service": "sekret"}
    )

    assert resp.status_code == 503
