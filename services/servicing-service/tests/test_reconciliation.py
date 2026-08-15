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

import psycopg2
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
    {
        "id": 1,
        "loan_id": 4471,
        "amount": 250.00,
        "created_at": dt.datetime(2026, 6, 1, 9, 14, 11),
    },
    {
        "id": 2,
        "loan_id": 5582,
        "amount": 410.50,
        "created_at": dt.datetime(2026, 6, 1, 9, 31, 4),
    },
    {
        "id": 3,
        "loan_id": 5582,
        "amount": 410.50,
        "created_at": dt.datetime(2026, 6, 1, 9, 31, 6),
    },
    {
        "id": 4,
        "loan_id": 4471,
        "amount": 99.99,
        "created_at": dt.datetime(2026, 6, 1, 11, 18, 45),
    },
    {
        "id": 5,
        "loan_id": 6011,
        "amount": 432.18,
        "created_at": dt.datetime(2026, 6, 2, 8, 0, 0),
    },
    {
        "id": 6,
        "loan_id": 4471,
        "amount": 250.00,
        "created_at": dt.datetime(2026, 6, 3, 9, 0, 0),
    },
    {
        "id": 7,
        "loan_id": 6011,
        "amount": 432.18,
        "created_at": dt.datetime(2026, 6, 3, 8, 0, 0),
    },
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
    """Stub the ledger side; defaults to the seeded June rows.

    Also sets a valid DUPLICATE_SUSPECT_WINDOW_SECONDS (D2(e)) so tests that reach
    duplicate detection succeed by default; pass window_seconds to override.
    """

    def _install(rows=SEEDED_LEDGER, window_seconds="120"):
        recorder = Recorder(rows)
        monkeypatch.setattr(reconciliation.db, "query", recorder)
        monkeypatch.setattr(
            reconciliation, "DUPLICATE_SUSPECT_WINDOW_SECONDS", window_seconds
        )
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


def test_v_amount_mismatch_same_loan_and_date_differing_amount(ledger, tmp_path):
    """V-AMOUNT-MISMATCH — ledger 250.00 vs settlement 249.99, same loan/date.

    Exact match requires equal amount_minor, so this pair never matches. Before
    AMOUNT_MISMATCH existed it fell out as one MISSING_IN_SETTLEMENT (25000) plus one
    unrelated MISSING_IN_LEDGER (24999), inflating gross_break_minor to 49999 for a
    1-minor-unit discrepancy. It must report as a single AMOUNT_MISMATCH of the delta
    instead, and never also as MISSING_IN_SETTLEMENT/MISSING_IN_LEDGER.
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
    path = write_settlement(tmp_path, ["2026-06-01,PR-100231,4471,249.99,capture"])

    result = reconciliation.reconcile(settlement_path=path)

    mismatches = klass(result, reconciliation.AMOUNT_MISMATCH)
    assert len(mismatches) == 1
    assert mismatches[0].amount_minor == 1
    assert mismatches[0].loan_id == 4471
    assert klass(result, reconciliation.MISSING_IN_SETTLEMENT) == []
    assert klass(result, reconciliation.MISSING_IN_LEDGER) == []
    assert result.gross_break_minor == 1
    assert result.exit_code == reconciliation.EXIT_BREAKS


def test_an_exact_match_is_never_reclassified_as_a_mismatch(ledger, tmp_path):
    """A perfect match always wins the first pass; a second, later-arriving capture
    on the same loan/date at a different amount is a true MISSING_IN_LEDGER, not a
    mismatch stolen from the row that already matched exactly."""
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
    path = write_settlement(
        tmp_path,
        [
            "2026-06-01,PR-1,4471,250.00,capture",
            "2026-06-01,PR-2,4471,99.99,capture",
        ],
    )

    result = reconciliation.reconcile(settlement_path=path)

    assert klass(result, reconciliation.AMOUNT_MISMATCH) == []
    missing = klass(result, reconciliation.MISSING_IN_LEDGER)
    assert len(missing) == 1
    assert missing[0].amount_minor == 9999


def test_a_ledger_row_before_the_window_still_matches_within_tolerance(
    ledger, tmp_path
):
    """D2(c) boundary fix (review finding, was pinned as a residual before this).

    A capture the day before `--from` that settles on `--from` is exactly the
    settlement lag the ±1 day tolerance exists to absorb. The matching candidate
    pool is widened by tolerance_days on each edge specifically so this pairs
    instead of falsely reporting MISSING_IN_LEDGER — turning normal cutoff timing
    into a customer-money break was the defect. TOTALS (net/per-loan/gross variance)
    still use the narrow, true window, so this cannot reintroduce the P1 defect
    (`ledger_total()` over the whole table against seven days of settlement) —
    see test_a_boundary_match_does_not_widen_the_totals_window below.
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

    assert klass(result, reconciliation.MISSING_IN_LEDGER) == []
    assert klass(result, reconciliation.MISSING_IN_SETTLEMENT) == []
    assert result.exit_code == reconciliation.EXIT_CLEAN


def test_a_settlement_row_after_the_window_still_matches_within_tolerance(
    ledger, tmp_path
):
    """The mirror case on the settlement side: a ledger row on `--to` that settles
    the day after `--to` is the same cutoff lag, just at the other edge."""
    ledger(
        [
            {
                "id": 1,
                "loan_id": 4471,
                "amount": 250.00,
                "created_at": dt.datetime(2026, 6, 30, 9, 0, 0),
            }
        ]
    )
    path = write_settlement(tmp_path, ["2026-07-01,PR-100231,4471,250.00,capture"])

    result = reconciliation.reconcile(
        from_date=dt.date(2026, 6, 1),
        to_date=dt.date(2026, 6, 30),
        settlement_path=path,
    )

    assert klass(result, reconciliation.MISSING_IN_LEDGER) == []
    assert klass(result, reconciliation.MISSING_IN_SETTLEMENT) == []
    assert result.exit_code == reconciliation.EXIT_CLEAN


def test_a_boundary_match_does_not_widen_the_totals_window(ledger, tmp_path):
    """The widened candidate pool is for matching only — net/per-loan/gross variance
    must still reflect the narrow, true window, or the boundary fix reintroduces the
    P1 defect (a total over a period wider than the settlement side) in reverse.

    The 5/31 ledger row here matches the 6/1 settlement row (no break), but since it
    falls outside the true window it must not be counted in the window's ledger
    total — net_variance_minor stays driven by the true-window sides only.
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

    # Settlement side (true window) carries 250.00; ledger side (true window)
    # carries nothing, since the only ledger row is outside window_from..window_to.
    assert result.net_variance_minor == -25000
    assert result.per_loan_absolute_minor == 25000


def test_zero_tolerance_does_not_widen_the_candidate_pool(ledger, tmp_path):
    """candidate_from/candidate_to collapse to window_from/window_to at ±0d — the
    boundary widening is proportional to tolerance_days, not a fixed extra day."""
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
        tolerance_days=0,
        settlement_path=path,
    )

    assert len(klass(result, reconciliation.MISSING_IN_LEDGER)) == 1
    assert klass(result, reconciliation.MISSING_IN_SETTLEMENT) == []


def test_a_true_window_settlement_row_beats_an_out_of_window_edge_candidate(
    ledger, tmp_path
):
    """Review finding: two same-loan/same-amount settlement rows straddle
    window_from (06-01, out of window; 06-02, the window start) and one exact
    ledger row lands on 06-02. The matcher used to accept the first candidate in
    date-sort order — the earlier, out-of-window 06-01 row — leaving the true
    06-02 row falsely unmatched and reported MISSING_IN_LEDGER. An exact-date,
    true-window match must always win over a tolerance/edge one.
    """
    ledger(
        [
            {
                "id": 1,
                "loan_id": 4471,
                "amount": 250.00,
                "created_at": dt.datetime(2026, 6, 2, 9, 0, 0),
            }
        ]
    )
    path = write_settlement(
        tmp_path,
        [
            "2026-06-01,PR-OLD,4471,250.00,capture",
            "2026-06-02,PR-NEW,4471,250.00,capture",
        ],
    )

    result = reconciliation.reconcile(
        from_date=dt.date(2026, 6, 2),
        to_date=dt.date(2026, 6, 2),
        settlement_path=path,
    )

    assert result.breaks == []
    assert result.exit_code == reconciliation.EXIT_CLEAN


def test_an_edge_exact_match_beats_a_true_window_amount_mismatch(ledger, tmp_path):
    """Review finding, REJECTED on the numbers — pinned so it is not re-litigated.

    A ledger row on 06-02 for 10000, an out-of-window capture on 06-01 for 10000, and
    a true-window capture on 06-02 for 10100. Review asked that the true-window row be
    ranked first, pairing 10000 against 10100 as a 100-minor-unit AMOUNT_MISMATCH
    instead of matching the edge row exactly.

    That reads the data the less safe way. Amount equality does not drift; settlement
    DATE does, which is the only reason a tolerance exists at all — so an exact-amount
    capture one day out is the settlement lag D2(c) widened the pool to absorb, and the
    10100 capture then has no ledger row behind it: money captured, never credited, the
    full 10100. Ranking the mismatch first reports a 100-minor-unit delta, and the
    10000 edge capture it displaces falls outside the true window, so the relevance
    filter drops it and nothing in this window's report points at the 10100 at all —
    a 100x understatement of the exposure on the same rows.

    Understating a break is the failure this control exists to prevent; overstating one
    costs an investigation that closes. The same precedence on the same-date variant is
    pinned by `test_an_exact_match_is_never_reclassified_as_a_mismatch`.
    """
    ledger(
        [
            {
                "id": 1,
                "loan_id": 4471,
                "amount": 100.00,
                "created_at": dt.datetime(2026, 6, 2, 9, 0, 0),
            }
        ]
    )
    path = write_settlement(
        tmp_path,
        [
            "2026-06-01,PR-EDGE,4471,100.00,capture",
            "2026-06-02,PR-TRUE,4471,101.00,capture",
        ],
    )

    result = reconciliation.reconcile(
        from_date=dt.date(2026, 6, 2),
        to_date=dt.date(2026, 6, 2),
        settlement_path=path,
    )

    assert result.matched_count == 1
    assert klass(result, reconciliation.AMOUNT_MISMATCH) == []
    missing = klass(result, reconciliation.MISSING_IN_LEDGER)
    assert len(missing) == 1
    assert missing[0].amount_minor == 10100
    assert missing[0].processor_ref == "PR-TRUE"
    assert result.exit_code == reconciliation.EXIT_BREAKS


def test_a_true_window_ledger_row_beats_an_out_of_window_edge_candidate(
    ledger, tmp_path
):
    """Mirror of the case above, on the ledger side. Two same-loan/same-amount
    ledger rows straddle window_from (05-31, out of window; 06-01, the window
    start) and there is only one settlement row, exactly on 06-01. Processing
    edge ledger rows before true-window ones would let the out-of-window 05-31
    row claim the only capture, leaving the true 06-01 row falsely unmatched.
    """
    ledger(
        [
            {
                "id": 1,
                "loan_id": 4471,
                "amount": 250.00,
                "created_at": dt.datetime(2026, 5, 31, 9, 0, 0),
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

    result = reconciliation.reconcile(
        from_date=dt.date(2026, 6, 1),
        to_date=dt.date(2026, 6, 1),
        settlement_path=path,
    )

    assert klass(result, reconciliation.MISSING_IN_SETTLEMENT) == []
    assert klass(result, reconciliation.MISSING_IN_LEDGER) == []


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
    assert len(result.duplicates) == 1
    assert result.duplicates[0].loan_id == 5582
    assert result.duplicates[0].amount_minor == 41050
    assert result.duplicates[0].gap_seconds == 2
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
    """V-SAMPLE-TIGHT — ±0d: 7 breaks, gross 257418, net unchanged at −88882.

    Also V-DUP-TOL-INVARIANT: DUPLICATE_SUSPECT is identical to the ±1d run — it does
    not depend on `tolerance_days` at all, since it never consults the settlement side.
    """
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
    assert len(result.duplicates) == 1
    assert result.duplicates[0].loan_id == 5582
    assert result.duplicates[0].amount_minor == 41050
    assert result.duplicates[0].gap_seconds == 2
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
    assert body["duplicate_suspect_count"] == 1


def test_fail_open_total_helpers_are_gone():
    """D2(g) + criterion 10 — `ledger_total`/`settlement_total` no longer exist.

    `settlement_total()` returned 0.0 for a missing file: a number reported over a
    file it never read.
    """
    assert not hasattr(reconciliation, "ledger_total")
    assert not hasattr(reconciliation, "settlement_total")


# --- Duplicate detection, independent of matching (D2(d)/(e)) --------------------


def test_v_dup_detect_reports_one_pair_regardless_of_settlement(ledger, tmp_path):
    """V-DUP-DETECT — two ledger rows, same loan/amount, 2s apart.

    The scan never consults the settlement side, so it fires even when the settlement
    file has nothing to do with the duplicated loan.
    """
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
    path = write_settlement(tmp_path, ["2026-06-01,PR-1,9999,1.00,capture"])

    result = reconciliation.reconcile(
        from_date=dt.date(2026, 6, 1), to_date=dt.date(2026, 6, 1), settlement_path=path
    )

    assert len(result.duplicates) == 1
    dup = result.duplicates[0]
    assert dup.loan_id == 5582
    assert dup.amount_minor == 41050
    assert dup.gap_seconds == 2
    assert dup.first_payment_id == 1
    assert dup.second_payment_id == 2


def test_v_dup_absorbed_matcher_still_matches_both_duplicate_rows(ledger, tmp_path):
    """V-DUP-ABSORBED — under ±1d the duplicate is invisible to `_match`; MISSING_IN_
    SETTLEMENT comes back empty. This is D2(d), the reason (e) is a separate check.
    """
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
    path = write_settlement(
        tmp_path,
        [
            "2026-06-01,PR-1,5582,410.50,capture",
            "2026-06-02,PR-2,5582,410.50,capture",
        ],
    )

    result = reconciliation.reconcile(
        from_date=dt.date(2026, 6, 1), to_date=dt.date(2026, 6, 2), settlement_path=path
    )

    assert klass(result, reconciliation.MISSING_IN_SETTLEMENT) == []
    assert klass(result, reconciliation.MISSING_IN_LEDGER) == []
    assert len(result.duplicates) == 1


def test_a_third_same_amount_row_pairs_once_not_twice(ledger, tmp_path):
    """Adjacent pairing: three rows 2s apart each report one pair and one clean row,
    not two overlapping pairs double-counting the middle row."""
    ledger(
        [
            {
                "id": i,
                "loan_id": 5582,
                "amount": 410.50,
                "created_at": dt.datetime(2026, 6, 1, 9, 31, 4 + 2 * (i - 1)),
            }
            for i in (1, 2, 3)
        ]
    )
    path = write_settlement(tmp_path, ["2026-06-01,PR-1,9999,1.00,capture"])

    result = reconciliation.reconcile(
        from_date=dt.date(2026, 6, 1), to_date=dt.date(2026, 6, 1), settlement_path=path
    )

    assert len(result.duplicates) == 1
    assert result.duplicates[0].first_payment_id == 1
    assert result.duplicates[0].second_payment_id == 2


def test_a_duplicate_pair_split_across_the_window_edge_is_still_detected(
    ledger, tmp_path
):
    """Review finding: a retry pair split across window_from midnight (23:59:59 the
    day before, 00:00:01 on window_from) is within DUPLICATE_SUSPECT_WINDOW_SECONDS
    of each other, but the narrow true-window ledger only ever holds the second row.
    A scan restricted to that narrow ledger would never see the pair -- exactly the
    seconds-to-minutes duplicate class this control exists to catch, missed at every
    scheduled daily window boundary.
    """
    ledger(
        [
            {
                "id": 1,
                "loan_id": 4471,
                "amount": 250.00,
                "created_at": dt.datetime(2026, 5, 31, 23, 59, 59),
            },
            {
                "id": 2,
                "loan_id": 4471,
                "amount": 250.00,
                "created_at": dt.datetime(2026, 6, 1, 0, 0, 1),
            },
        ],
        window_seconds="120",
    )
    path = write_settlement(tmp_path, ["2026-06-01,PR-1,4471,250.00,capture"])

    result = reconciliation.reconcile(
        from_date=dt.date(2026, 6, 1),
        to_date=dt.date(2026, 6, 30),
        settlement_path=path,
    )

    assert len(result.duplicates) == 1
    dup = result.duplicates[0]
    assert dup.first_payment_id == 1
    assert dup.second_payment_id == 2
    assert dup.gap_seconds == 2


def test_a_duplicate_pair_entirely_outside_the_window_is_not_reported(ledger, tmp_path):
    """The mirror case: both rows of a pair sit outside window_from..window_to. It
    is not this window's signal to report -- it belongs to whichever window
    actually contains it, not a window it merely lands near."""
    ledger(
        [
            {
                "id": 1,
                "loan_id": 4471,
                "amount": 250.00,
                "created_at": dt.datetime(2026, 5, 31, 23, 59, 58),
            },
            {
                "id": 2,
                "loan_id": 4471,
                "amount": 250.00,
                "created_at": dt.datetime(2026, 5, 31, 23, 59, 59),
            },
        ],
        window_seconds="120",
    )
    path = write_settlement(tmp_path, ["2026-06-01,PR-1,9999,1.00,capture"])

    result = reconciliation.reconcile(
        from_date=dt.date(2026, 6, 1),
        to_date=dt.date(2026, 6, 30),
        settlement_path=path,
    )

    assert result.duplicates == []


def test_a_duplicate_only_run_does_not_exit_clean(ledger, tmp_path):
    """D2(g), review finding. Matching absorbs the duplicate under the tolerance, so
    `breaks` is empty and both sides tie out to zero variance -- the run's ONLY
    finding is the duplicate pair. An exit code derived from `breaks` alone reported
    that as reconciled-clean, so the cron or operator keying off the exit code missed
    exactly the double-charge signal (e) exists to raise.
    """
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
    path = write_settlement(
        tmp_path,
        [
            "2026-06-01,PR-1,5582,410.50,capture",
            "2026-06-02,PR-2,5582,410.50,capture",
        ],
    )

    result = reconciliation.reconcile(
        from_date=dt.date(2026, 6, 1), to_date=dt.date(2026, 6, 2), settlement_path=path
    )

    # The duplicate is the only finding: nothing else in the run is out of place.
    assert result.breaks == []
    assert result.net_variance_minor == 0
    assert result.per_loan_absolute_minor == 0
    assert len(result.duplicates) == 1
    assert result.exit_code == reconciliation.EXIT_BREAKS


def test_a_boundary_match_is_the_only_way_variance_survives_a_clean_exit(
    ledger, tmp_path
):
    """D2(g) pin, against a review finding that asked for any nonzero variance to
    force a non-clean exit.

    Nonzero variance with an empty `breaks` list has exactly one cause: a match that
    pairs across the window edge, which is the settlement lag the tolerance exists to
    absorb (see test_a_ledger_row_before_the_window_still_matches_within_tolerance).
    Every other suppressed row sits outside the narrow window on BOTH sides, so it
    contributes nothing to the totals either. The money is not unaccounted for -- it
    reconciles into the adjacent window -- so forcing exit 1 here would report
    ordinary cutoff timing as a break, which is the defect the boundary fix removed.

    This pins the pairing (variance nonzero AND clean AND explained) so a later change
    cannot quietly make a clean exit hide variance from some OTHER cause.
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

    assert result.breaks == []
    assert result.duplicates == []
    assert result.matched_count == 1
    # Nonzero purely because the ledger side of the matched pair sits in May.
    assert result.net_variance_minor == -25000
    assert result.exit_code == reconciliation.EXIT_CLEAN


def test_an_outside_pair_does_not_consume_the_row_a_window_retry_pairs_with(
    ledger, tmp_path
):
    """D2(e) boundary, review finding. Three retries of the same charge, the first two
    just before window_from and the third just inside it, every gap inside
    DUPLICATE_SUSPECT_WINDOW_SECONDS.

    Adjacent pairing used to claim the two outside rows first and advance past them,
    so the inside row was never compared to its immediate predecessor; the relevance
    filter then dropped the outside-only pair and this window reported no duplicate at
    all. The third charge went unreported by every window: the previous window's run
    reports the first pair and treats the third row as clean, and this one saw
    nothing. Pair selection must prefer a pair that touches the true window over one
    that does not.
    """
    ledger(
        [
            {
                "id": 1,
                "loan_id": 4471,
                "amount": 250.00,
                "created_at": dt.datetime(2026, 5, 31, 23, 59, 0),
            },
            {
                "id": 2,
                "loan_id": 4471,
                "amount": 250.00,
                "created_at": dt.datetime(2026, 5, 31, 23, 59, 30),
            },
            {
                "id": 3,
                "loan_id": 4471,
                "amount": 250.00,
                "created_at": dt.datetime(2026, 6, 1, 0, 0, 10),
            },
        ],
        window_seconds="120",
    )
    path = write_settlement(tmp_path, ["2026-06-01,PR-1,9999,1.00,capture"])

    result = reconciliation.reconcile(
        from_date=dt.date(2026, 6, 1),
        to_date=dt.date(2026, 6, 30),
        settlement_path=path,
    )

    assert len(result.duplicates) == 1
    dup = result.duplicates[0]
    # The pair that reaches into the window, not the one wholly outside it.
    assert (dup.first_payment_id, dup.second_payment_id) == (2, 3)
    assert dup.gap_seconds == 40
    assert result.exit_code == reconciliation.EXIT_BREAKS


# --- Duplicate-window config, fail closed (D2(e)) ---------------------------------


def test_missing_duplicate_window_env_aborts(monkeypatch, tmp_path):
    monkeypatch.setattr(reconciliation.db, "query", Recorder([]))
    monkeypatch.setattr(reconciliation, "DUPLICATE_SUSPECT_WINDOW_SECONDS", "")
    path = write_settlement(tmp_path, ["2026-06-01,PR-1,4471,250.00,capture"])

    with pytest.raises(reconciliation.ReconciliationAbort) as excinfo:
        reconciliation.reconcile(settlement_path=path)

    assert "DUPLICATE_SUSPECT_WINDOW_SECONDS" in str(excinfo.value)


@pytest.mark.parametrize("bad_value", ["abc", "0", "-5", "99999999999999999999"])
def test_invalid_duplicate_window_env_aborts(monkeypatch, tmp_path, bad_value):
    """Includes an absurdly large value (teeth check): unguarded, it reaches
    `timedelta(seconds=...)` in `_find_duplicate_suspects` and raises OverflowError
    instead of ReconciliationAbort — a fail-closed contract violation on a
    misconfigured operator value."""
    monkeypatch.setattr(reconciliation.db, "query", Recorder([]))
    monkeypatch.setattr(reconciliation, "DUPLICATE_SUSPECT_WINDOW_SECONDS", bad_value)
    path = write_settlement(tmp_path, ["2026-06-01,PR-1,4471,250.00,capture"])

    with pytest.raises(reconciliation.ReconciliationAbort):
        reconciliation.reconcile(settlement_path=path)


def test_malformed_ledger_row_aborts_instead_of_crashing(monkeypatch, tmp_path):
    """Teeth check: `payments.created_at`/`loan_id` carry no NOT NULL constraint
    (db/init/001_schema.sql), so a NULL row is reachable without any query failure.
    Unguarded, `None.date()` raises a bare AttributeError past the route's abort
    handling into the generic 500 — the same fail-closed violation as an unreachable
    database, just triggered by row content instead of a connection failure."""
    monkeypatch.setattr(
        reconciliation.db,
        "query",
        Recorder([{"id": 1, "loan_id": 4471, "amount": 250.00, "created_at": None}]),
    )
    monkeypatch.setattr(reconciliation, "DUPLICATE_SUSPECT_WINDOW_SECONDS", "120")
    path = write_settlement(tmp_path, ["2026-06-01,PR-1,4471,250.00,capture"])

    with pytest.raises(reconciliation.ReconciliationAbort) as excinfo:
        reconciliation.reconcile(settlement_path=path)

    assert "created_at is missing" in str(excinfo.value)


def test_ledger_row_with_null_loan_id_aborts_instead_of_crashing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        reconciliation.db,
        "query",
        Recorder(
            [
                {
                    "id": 1,
                    "loan_id": None,
                    "amount": 250.00,
                    "created_at": dt.datetime(2026, 6, 1, 9, 0, 0),
                }
            ]
        ),
    )
    monkeypatch.setattr(reconciliation, "DUPLICATE_SUSPECT_WINDOW_SECONDS", "120")
    path = write_settlement(tmp_path, ["2026-06-01,PR-1,4471,250.00,capture"])

    with pytest.raises(reconciliation.ReconciliationAbort) as excinfo:
        reconciliation.reconcile(settlement_path=path)

    assert "loan_id is missing" in str(excinfo.value)


# --- Ledger query failure aborts, never reaches the route's generic 500 ----------


def test_ledger_query_failure_aborts_instead_of_a_bare_500(monkeypatch, tmp_path):
    """A DB error is a verifier that could not verify — ReconciliationAbort, not a
    raw psycopg2 exception reaching FastAPI's unhandled-exception 500."""

    def _boom(sql, params=None):
        raise psycopg2.OperationalError("could not connect to server")

    monkeypatch.setattr(reconciliation.db, "query", _boom)
    path = write_settlement(tmp_path, ["2026-06-01,PR-1,4471,250.00,capture"])

    with pytest.raises(reconciliation.ReconciliationAbort) as excinfo:
        reconciliation.reconcile(settlement_path=path)

    assert "ledger query failed" in str(excinfo.value)


def test_peek_reports_ledger_query_failure_as_503_not_500(monkeypatch, tmp_path):
    def _boom(sql, params=None):
        raise psycopg2.OperationalError("could not connect to server")

    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    path = write_settlement(tmp_path, ["2026-06-01,PR-1,4471,250.00,capture"])
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE", path)
    monkeypatch.setattr(reconciliation.db, "query", _boom)

    resp = TestClient(app).get(
        "/reconciliation/peek", headers={"X-Internal-Service": "sekret"}
    )

    assert resp.status_code == 503
