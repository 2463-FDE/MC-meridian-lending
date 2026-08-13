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

import datetime as dt
from pathlib import Path

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

    with pytest.raises(reconciliation.ReconciliationAbort) as excinfo:
        reconciliation.reconcile(settlement_path=absent)

    assert absent in str(excinfo.value)


def test_an_empty_file_aborts(tmp_path):
    """Header only, zero data rows — nothing was compared, so there is no total."""
    path = write_settlement(tmp_path, [])

    with pytest.raises(reconciliation.ReconciliationAbort):
        reconciliation.load_settlement(path)


def test_undecodable_bytes_abort_instead_of_crashing(tmp_path):
    """`open()` succeeds on bad bytes; only iterating the reader raises.

    `UnicodeDecodeError` is a `ValueError`, not an `OSError` — the read loop must
    catch it explicitly or it reaches the route as an uncontrolled 500 instead of
    the intended `ReconciliationAbort` -> 503.
    """
    path = tmp_path / "settlement.csv"
    path.write_bytes(
        SETTLEMENT_HEADER.encode() + b"2026-06-01,PR-1,4471,\xff\xfe,capture\n"
    )

    with pytest.raises(reconciliation.ReconciliationAbort):
        reconciliation.load_settlement(str(path))


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


def test_peek_reports_undecodable_settlement_bytes_as_503_not_500(
    tmp_path, monkeypatch
):
    """Same fail-open at the HTTP boundary, for the decode-error path specifically."""
    path = tmp_path / "settlement.csv"
    path.write_bytes(
        SETTLEMENT_HEADER.encode() + b"2026-06-01,PR-1,4471,\xff\xfe,capture\n"
    )
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE", str(path))

    resp = TestClient(app).get(
        "/reconciliation/peek", headers={"X-Internal-Service": "sekret"}
    )

    assert resp.status_code == 503


# --- Matching and classification (D2(c), D2(f)) ----------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE_SETTLEMENT = REPO_ROOT / "db" / "settlement.csv"

# `db/init/002_seed.sql:69-75` — the June rows the sample settlement file covers.
# `payments.amount` is DOUBLE PRECISION, so psycopg2 hands back Python floats; the
# values are carried here in that type deliberately.
SEEDED_LEDGER = [
    {"id": 1, "loan_id": 4471, "amount": 250.00, "created_at": dt.datetime(2026, 6, 1, 9, 14, 11)},
    {"id": 2, "loan_id": 5582, "amount": 410.50, "created_at": dt.datetime(2026, 6, 1, 9, 31, 4)},
    {"id": 3, "loan_id": 5582, "amount": 410.50, "created_at": dt.datetime(2026, 6, 1, 9, 31, 6)},
    {"id": 4, "loan_id": 4471, "amount": 99.99, "created_at": dt.datetime(2026, 6, 1, 11, 18, 45)},
    {"id": 5, "loan_id": 6011, "amount": 432.18, "created_at": dt.datetime(2026, 6, 2, 8, 0, 0)},
    {"id": 6, "loan_id": 4471, "amount": 250.00, "created_at": dt.datetime(2026, 6, 3, 9, 0, 0)},
    {"id": 7, "loan_id": 6011, "amount": 432.18, "created_at": dt.datetime(2026, 6, 3, 8, 0, 0)},
]


class Recorder:
    """Stub for `app.db.query` that records every statement it was handed."""

    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    def __call__(self, sql, params=None):
        self.statements.append(sql)
        return list(self.rows)

    @property
    def writes(self):
        return [
            s
            for s in self.statements
            if not s.strip().upper().lstrip("(").startswith("SELECT")
        ]


@pytest.fixture
def ledger(monkeypatch):
    """Stub the ledger side; defaults to the seeded June rows."""

    def _install(rows=SEEDED_LEDGER):
        recorder = Recorder(rows)
        monkeypatch.setattr(reconciliation.db, "query", recorder)
        return recorder

    return _install


def klass(result, name):
    return [b for b in result.breaks if b.break_class == name]


def minor(breaks):
    return sum(b.amount_minor for b in breaks)


def test_v_match_exact_pair_is_matched_and_contributes_nothing(ledger, tmp_path):
    """V-MATCH — same loan, amount and date on both sides."""
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
    assert result.net_variance_minor == 0
    assert result.exit_code == reconciliation.EXIT_CLEAN


def test_v_window_next_day_settlement_still_matches(ledger, tmp_path):
    """V-WINDOW — ledger 06-01 against settlement 06-02 is inside the ±1 day tolerance.

    The window is explicit here because the vector is about the *matching rule*, not
    about window derivation: the derived default is the settlement file's own range
    (06-02 only), which would put the ledger row out of scope before matching ever
    ran. See `test_a_ledger_row_before_the_window_is_out_of_scope_not_matched`.
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
    path = write_settlement(tmp_path, ["2026-06-02,PR-100231,4471,250.00,capture"])

    result = reconciliation.reconcile(
        from_date=dt.date(2026, 6, 1), to_date=dt.date(2026, 6, 2), settlement_path=path
    )

    assert result.breaks == []


def test_v_window_out_two_days_apart_is_a_break_on_each_side(ledger, tmp_path):
    """V-WINDOW-OUT — two days apart falls outside ±1 day; one break each side."""
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
    path = write_settlement(tmp_path, ["2026-06-03,PR-100231,4471,250.00,capture"])

    result = reconciliation.reconcile(
        from_date=dt.date(2026, 6, 1), to_date=dt.date(2026, 6, 3), settlement_path=path
    )

    assert len(klass(result, reconciliation.MISSING_IN_SETTLEMENT)) == 1
    assert len(klass(result, reconciliation.MISSING_IN_LEDGER)) == 1
    assert result.exit_code == reconciliation.EXIT_BREAKS


def test_a_ledger_row_before_the_window_is_out_of_scope_not_matched(ledger, tmp_path):
    """Pins a known boundary property of D2(a) so it is a decision, not an accident.

    The window applies to both sides identically (D2(a)), so a capture on the day
    before `--from` that settles on `--from` is out of scope and its settlement row
    reports as MISSING_IN_LEDGER. The ±1 day tolerance operates *inside* the window,
    not across its edge.

    The alternative — widening the ledger fetch by the tolerance — makes the two
    sides span different periods, which is the P1 defect (`ledger_total()` over the
    whole table against seven days of settlement) reintroduced at the edge. The
    caller controls this by choosing the window: a month-end run over a calendar
    month, not over the file's exact min/max.

    This is a residual for D3's report and D6's runbook to state, not a defect to
    fix silently here.
    """
    ledger(
        [
            {
                "id": 1,
                "loan_id": 4471,
                "amount": 250.00,
                "created_at": dt.datetime(2026, 5, 31, 23, 50, 0),
            }
        ]
    )
    path = write_settlement(tmp_path, ["2026-06-01,PR-100231,4471,250.00,capture"])

    result = reconciliation.reconcile(
        from_date=dt.date(2026, 6, 1),
        to_date=dt.date(2026, 6, 30),
        settlement_path=path,
    )

    assert len(klass(result, reconciliation.MISSING_IN_LEDGER)) == 1
    assert klass(result, reconciliation.MISSING_IN_SETTLEMENT) == []


def test_v_missing_ledger_settled_captures_with_no_ledger_row(ledger, tmp_path):
    """V-MISSING-LEDGER — PR-100290 and PR-100311, 25000 each, no ledger counterpart."""
    ledger([])
    path = write_settlement(
        tmp_path,
        [
            "2026-06-05,PR-100290,4471,250.00,capture",
            "2026-06-06,PR-100311,4471,250.00,capture",
        ],
    )

    result = reconciliation.reconcile(settlement_path=path)

    missing = klass(result, reconciliation.MISSING_IN_LEDGER)
    assert len(missing) == 2
    assert minor(missing) == 50000


def test_v_refund_is_its_own_class_never_missing_in_ledger(ledger, tmp_path):
    """V-REFUND — a settlement refund is a schema limitation, not a lost payment."""
    ledger([])
    path = write_settlement(tmp_path, ["2026-06-05,PR-100299,6011,432.18,refund"])

    result = reconciliation.reconcile(settlement_path=path)

    refunds = klass(result, reconciliation.REFUND_UNREPRESENTED)
    assert len(refunds) == 1
    assert refunds[0].amount_minor == 43218
    assert klass(result, reconciliation.MISSING_IN_LEDGER) == []


def test_v_sample_full_run_matches_the_pinned_figures(ledger):
    """V-SAMPLE / acceptance criterion 4 — the whole sample at the ±1 day default.

    5 breaks: MISSING_IN_LEDGER 4 / 132100, REFUND_UNREPRESENTED 1 / 43218,
    MISSING_IN_SETTLEMENT 0. Plus 1 DUPLICATE_SUSPECT. Exit 1.
    """
    recorder = ledger()

    result = reconciliation.reconcile(settlement_path=str(SAMPLE_SETTLEMENT))

    assert len(result.breaks) == 5
    assert len(klass(result, reconciliation.MISSING_IN_LEDGER)) == 4
    assert minor(klass(result, reconciliation.MISSING_IN_LEDGER)) == 132100
    assert len(klass(result, reconciliation.REFUND_UNREPRESENTED)) == 1
    assert minor(klass(result, reconciliation.REFUND_UNREPRESENTED)) == 43218
    assert klass(result, reconciliation.MISSING_IN_SETTLEMENT) == []
    assert result.exit_code == reconciliation.EXIT_BREAKS
    assert recorder.writes == []


def test_v_sample_reports_all_three_labelled_figures(ledger):
    """Acceptance criterion 5 — net, per-loan absolute, and gross, in one summary."""
    ledger()

    result = reconciliation.reconcile(settlement_path=str(SAMPLE_SETTLEMENT))

    assert result.net_variance_minor == -88882
    assert result.per_loan_absolute_minor == 175318
    assert result.gross_break_minor == 175318


def test_v_sample_tight_zero_day_window(ledger):
    """V-SAMPLE-TIGHT — ±0d: 7 breaks, gross 257418, net unchanged at −88882."""
    ledger()

    result = reconciliation.reconcile(
        settlement_path=str(SAMPLE_SETTLEMENT), tolerance_days=0
    )

    assert len(result.breaks) == 7
    assert len(klass(result, reconciliation.MISSING_IN_LEDGER)) == 5
    assert minor(klass(result, reconciliation.MISSING_IN_LEDGER)) == 173150
    assert len(klass(result, reconciliation.MISSING_IN_SETTLEMENT)) == 1
    assert minor(klass(result, reconciliation.MISSING_IN_SETTLEMENT)) == 41050
    assert len(klass(result, reconciliation.REFUND_UNREPRESENTED)) == 1
    assert result.gross_break_minor == 257418
    assert result.net_variance_minor == -88882


def test_surplus_on_one_side_is_reported_as_a_count_difference_not_a_guess(
    ledger, tmp_path
):
    """D2(c) — three identical ledger rows against two identical settlement rows.

    The job never guesses which of the identical rows is the orphan; it reports that
    one is unmatched.
    """
    ledger(
        [
            {
                "id": i,
                "loan_id": 4471,
                "amount": 250.00,
                "created_at": dt.datetime(2026, 6, 1, 9, i, 0),
            }
            for i in (1, 2, 3)
        ]
    )
    path = write_settlement(
        tmp_path,
        [
            "2026-06-01,PR-1,4471,250.00,capture",
            "2026-06-01,PR-2,4471,250.00,capture",
        ],
    )

    result = reconciliation.reconcile(settlement_path=path)

    assert len(klass(result, reconciliation.MISSING_IN_SETTLEMENT)) == 1
    assert klass(result, reconciliation.MISSING_IN_LEDGER) == []


# --- Comparison window -----------------------------------------------------------


def test_window_defaults_to_the_settlement_files_own_date_range(ledger, tmp_path):
    """D2(a) — the default window is derived, not assumed."""
    ledger([])
    path = write_settlement(
        tmp_path,
        [
            "2026-06-02,PR-1,4471,250.00,capture",
            "2026-06-05,PR-2,4471,250.00,capture",
        ],
    )

    result = reconciliation.reconcile(settlement_path=path)

    assert result.window_from == dt.date(2026, 6, 2)
    assert result.window_to == dt.date(2026, 6, 5)


def test_explicit_window_excludes_settlement_rows_outside_it(ledger, tmp_path):
    """D2(a) — an explicit --from/--to overrides the derived window on both sides."""
    ledger([])
    path = write_settlement(
        tmp_path,
        [
            "2026-06-02,PR-1,4471,250.00,capture",
            "2026-06-09,PR-2,4471,250.00,capture",
        ],
    )

    result = reconciliation.reconcile(
        from_date=dt.date(2026, 6, 1), to_date=dt.date(2026, 6, 3), settlement_path=path
    )

    assert result.window_from == dt.date(2026, 6, 1)
    assert result.window_to == dt.date(2026, 6, 3)
    assert len(klass(result, reconciliation.MISSING_IN_LEDGER)) == 1


def test_ledger_rows_outside_the_window_are_excluded(ledger, tmp_path):
    """D2(a) — "on both sides".

    The window is a property of the job, not of the SQL string: a ledger row outside
    it is excluded even when the query hands it back. This is the P1 defect in
    miniature — `ledger_total()` summed the whole `payments` table, including the
    ~600 bulk-seeded 2026-05 rows, against seven days of settlement.
    """
    ledger(
        [
            {
                "id": 1,
                "loan_id": 7000,
                "amount": 100.00,
                "created_at": dt.datetime(2026, 5, 1, 9, 0, 0),
            },
            {
                "id": 2,
                "loan_id": 4471,
                "amount": 250.00,
                "created_at": dt.datetime(2026, 6, 1, 9, 0, 0),
            },
        ]
    )
    path = write_settlement(tmp_path, ["2026-06-01,PR-1,4471,250.00,capture"])

    result = reconciliation.reconcile(settlement_path=path)

    assert result.breaks == []
    assert result.net_variance_minor == 0


# --- Read-only (D2(h)) -----------------------------------------------------------


def test_v_readonly_job_issues_select_only(ledger):
    """V-READONLY / criterion 9 — every executed statement is a SELECT."""
    recorder = ledger()

    reconciliation.reconcile(settlement_path=str(SAMPLE_SETTLEMENT))

    assert recorder.statements, "the job never queried the ledger at all"
    assert recorder.writes == [], f"job issued non-SELECT statements: {recorder.writes}"


def test_the_ledger_query_never_selects_card_data(ledger):
    """Spec's redaction note — `payments.pan` and `payments.cvv` are never selected."""
    recorder = ledger()

    reconciliation.reconcile(settlement_path=str(SAMPLE_SETTLEMENT))

    for sql in recorder.statements:
        lowered = sql.lower()
        assert "pan" not in lowered
        assert "cvv" not in lowered


def test_peek_returns_the_break_summary_to_an_internal_caller(ledger, monkeypatch):
    """Acceptance criterion 10 — the route reports breaks, not two totals."""
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE", str(SAMPLE_SETTLEMENT))
    ledger()

    resp = TestClient(app).get(
        "/reconciliation/peek", headers={"X-Internal-Service": "sekret"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["break_counts"][reconciliation.MISSING_IN_LEDGER] == 4
    assert body["net_variance_minor"] == -88882
    assert body["exit_code"] == reconciliation.EXIT_BREAKS
    assert "ledger_total" not in body
    assert "settlement_total" not in body


def test_fail_open_total_helpers_are_gone():
    """D2(g) + criterion 10 — `ledger_total`/`settlement_total` no longer exist.

    `settlement_total()` returned 0.0 for a missing file: a number reported over a
    file it never read.
    """
    assert not hasattr(reconciliation, "ledger_total")
    assert not hasattr(reconciliation, "settlement_total")
