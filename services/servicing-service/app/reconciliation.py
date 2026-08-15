"""Row-level settlement reconciliation — closes debt D7.

Spec: `docs/spec-observability-week7.md` §D2. ADR 0015 records the decisions.

Both total-only helpers are gone. `ledger_total()` summed the whole `payments` table —
including the bulk-seeded 2026-05 rows — against a settlement file covering three loans
over seven days, so the subtraction was meaningless before any defect was considered.
`settlement_total()` netted the file into one figure, which cannot say WHICH row is
missing. Keeping either beside the real comparison would reproduce the drift the
fee-schedule loader was built to end.

What replaces them: a windowed, row-level comparison in integer minor units, with an
explicit matching rule, five break classes, and an abort that is distinct from
"reconciled, no breaks".

Read-only by construction (D2(h)): every statement it issues is a SELECT. It never
corrects a balance — that would be an unauthorized money movement with no maker-checker,
on the same unlocked read-modify-write that is debt D3.

D2(d)/(e): the ±1 day tolerance defeats duplicate detection on its own — a same-day
double charge matches under it and disappears from `_match`'s output. `_find_duplicate_suspects`
runs a second, matching-independent pass over the ledger side alone, so a duplicate can
never hide behind the tolerance that exists for a different reason (settlement lag).
"""

import csv
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

import psycopg2

from . import db
from .config import (
    DUPLICATE_SUSPECT_WINDOW_SECONDS,
    MAX_DUPLICATE_SUSPECT_WINDOW_SECONDS,
    SETTLEMENT_FILE,
)

# D2(c). The ledger stamps `created_at` at capture and the processor stamps
# `settlement_date` at settlement; no cut-off convention has been confirmed by the
# client (spec Client Questions Q5). Named, not buried in a comparison.
MATCH_TOLERANCE_DAYS = 1

# D2(f). Every unmatched row lands in exactly one of these.
MISSING_IN_LEDGER = "MISSING_IN_LEDGER"
MISSING_IN_SETTLEMENT = "MISSING_IN_SETTLEMENT"
REFUND_UNREPRESENTED = "REFUND_UNREPRESENTED"
AMOUNT_MISMATCH = "AMOUNT_MISMATCH"

# D2(e). A signal, not a break class: it never enters `breaks` or any variance figure
# (the duplicate's money is already accounted for on whichever side it landed).
DUPLICATE_SUSPECT = "DUPLICATE_SUSPECT"

# D2(g), mirroring `scripts/prove_test.sh`'s convention. "Could not check" is never
# reported as 0, and never as 1 either: a zero-break result from a run that read
# nothing is the failure this control exists to prevent.
EXIT_CLEAN = 0
EXIT_BREAKS = 1
EXIT_ABORT = 2

CAPTURE = "capture"
REFUND = "refund"
SETTLEMENT_TYPES = (CAPTURE, REFUND)
REQUIRED_COLUMNS = ("settlement_date", "processor_ref", "loan_id", "amount", "type")

# A money literal, and nothing else. `Decimal` on its own accepts `1_000.00`, `+250.00`,
# `2.5E+2` and `NaN` — `Decimal("2.5E+2")` equals 250 and would compare equal to a real
# figure while reading nothing like one. Same posture as the disclosure figure check.
_PLAIN_DECIMAL = re.compile(r"^\d+(\.\d+)?$")
_MINOR_UNITS_PER_MAJOR = Decimal(100)


class ReconciliationAbort(Exception):
    """The settlement file could not be read — exit 2, never 0 and never 1.

    Raised when the file is absent, unreadable, empty, missing a required column, or
    holds a row that does not parse. A verifier must never report a result for a path
    it did not verify.
    """


@dataclass(frozen=True)
class SettlementRow:
    settlement_date: date
    processor_ref: str
    loan_id: int
    amount_minor: int
    row_type: str


def _minor_units(raw, *, source: str) -> int:
    """Parse a money value to integer cents via `Decimal`, never binary float.

    `source` names the row for the abort message. Sub-cent precision aborts rather
    than rounding: a verifier that silently moves a figure is not verifying it.
    """
    if raw is None:
        raise ReconciliationAbort(f"{source}: amount is missing")
    text = str(raw).strip()
    if not _PLAIN_DECIMAL.match(text):
        raise ReconciliationAbort(
            f"{source}: amount {text!r} is not a plain decimal amount"
        )
    try:
        value = Decimal(text)
    except InvalidOperation:  # pragma: no cover - the regex already rejects these
        raise ReconciliationAbort(f"{source}: amount {text!r} does not parse")
    scaled = value * _MINOR_UNITS_PER_MAJOR
    if scaled != scaled.to_integral_value():
        raise ReconciliationAbort(
            f"{source}: amount {text!r} carries sub-cent precision and is not rounded"
        )
    return int(scaled)


def _parse_settlement_row(raw: dict, source: str) -> SettlementRow:
    for column in REQUIRED_COLUMNS:
        if raw.get(column) is None or not str(raw[column]).strip():
            raise ReconciliationAbort(f"{source}: column {column!r} is missing a value")

    raw_date = str(raw["settlement_date"]).strip()
    try:
        settlement_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        raise ReconciliationAbort(
            f"{source}: settlement_date {raw_date!r} does not parse"
        )

    raw_loan = str(raw["loan_id"]).strip()
    if not raw_loan.isdigit():
        raise ReconciliationAbort(f"{source}: loan_id {raw_loan!r} does not parse")

    row_type = str(raw["type"]).strip()
    if row_type not in SETTLEMENT_TYPES:
        # An unmodelled type (chargeback, reversal) is not silently dropped: it is
        # money this code cannot classify, so it cannot claim to have read the file.
        raise ReconciliationAbort(
            f"{source}: type {row_type!r} is not one of {SETTLEMENT_TYPES}"
        )

    return SettlementRow(
        settlement_date=settlement_date,
        processor_ref=str(raw["processor_ref"]).strip(),
        loan_id=int(raw_loan),
        amount_minor=_minor_units(raw["amount"], source=source),
        row_type=row_type,
    )


def load_settlement(path: str) -> list:
    """Read the settlement file into typed rows, or abort. Never a partial read."""
    if not os.path.exists(path):
        raise ReconciliationAbort(f"settlement file not found: {path}")
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            columns = reader.fieldnames or []
            missing = [c for c in REQUIRED_COLUMNS if c not in columns]
            if missing:
                raise ReconciliationAbort(
                    f"settlement file {path} is missing required column(s): "
                    f"{', '.join(missing)}"
                )
            rows = [
                _parse_settlement_row(raw, f"{path} line {lineno}")
                for lineno, raw in enumerate(reader, start=2)
            ]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReconciliationAbort(f"settlement file {path} could not be read: {exc}")

    if not rows:
        raise ReconciliationAbort(f"settlement file {path} has no data rows")
    return rows


@dataclass(frozen=True)
class LedgerRow:
    payment_id: int
    loan_id: int
    amount_minor: int
    created_at: datetime


@dataclass(frozen=True)
class Break:
    """One finding. `amount_minor` is the value at stake, always integer cents."""

    break_class: str
    loan_id: int
    amount_minor: int
    occurred_on: date
    processor_ref: Optional[str] = None
    payment_id: Optional[int] = None
    detail: str = ""


@dataclass(frozen=True)
class DuplicateSuspect:
    """One pair from `_find_duplicate_suspects`. Signal only — see `DUPLICATE_SUSPECT`."""

    loan_id: int
    amount_minor: int
    occurred_on: date
    gap_seconds: int
    first_payment_id: int
    second_payment_id: int


@dataclass(frozen=True)
class ReconciliationResult:
    window_from: date
    window_to: date
    tolerance_days: int
    matched_count: int
    breaks: list = field(default_factory=list)
    # D2(e). Reported separately from `breaks` — see `DuplicateSuspect`.
    duplicates: list = field(default_factory=list)
    # D2(b): every figure is an int of minor units.
    net_variance_minor: int = 0
    per_loan_absolute_minor: int = 0
    gross_break_minor: int = 0
    # Per-side counts and totals, so a reader of the report can see what was compared
    # rather than take the variance on trust (D3(b)).
    ledger_row_count: int = 0
    ledger_total_minor: int = 0
    settlement_row_count: int = 0
    settlement_captures_minor: int = 0
    settlement_refunds_minor: int = 0

    @property
    def settlement_net_minor(self) -> int:
        return self.settlement_captures_minor - self.settlement_refunds_minor

    @property
    def exit_code(self) -> int:
        """D2(g). Driven by `duplicates` as well as `breaks` (review finding).

        A duplicate charge absorbed by matching leaves `breaks` empty while
        `duplicates` holds the whole finding, so an exit code derived from `breaks`
        alone reported the run clean and the cron or operator keying off it missed
        precisely the double-charge signal D2(e) exists to raise. A duplicate is
        still not a break and still enters no variance figure — it changes the
        status, not the arithmetic.

        Deliberately NOT driven by the variance figures: nonzero variance with an
        empty `breaks` list has one cause, a match pairing across the window edge,
        which is the settlement lag the tolerance absorbs by design (D2(c), and
        `test_a_boundary_match_is_the_only_way_variance_survives_a_clean_exit`).
        That money reconciles into the adjacent window rather than going missing.
        """
        if self.breaks or self.duplicates:
            return EXIT_BREAKS
        return EXIT_CLEAN


def load_ledger(window_from: date, window_to: date) -> list:
    """The capture side, inside the window. SELECT only; `pan`/`cvv` are never read.

    A query failure (Postgres unreachable, statement error) is a verifier that could
    not verify, not an unclassified server error — it aborts the same as an unreadable
    settlement file, rather than reaching the route's generic 500 handler.
    """
    try:
        rows = db.query(
            "SELECT id, loan_id, amount, created_at "
            "FROM payments "
            "WHERE created_at >= %s AND created_at < %s "
            "ORDER BY loan_id, amount, created_at, id",
            (window_from, window_to + timedelta(days=1)),
        )
    except psycopg2.Error as exc:
        raise ReconciliationAbort(
            f"ledger query failed for window {window_from}..{window_to}: {exc}"
        )
    ledger = []
    for row in rows:
        # `payments.created_at`/`loan_id` carry no NOT NULL constraint (db/init/
        # 001_schema.sql), so a malformed row is reachable without a query failure.
        # Same fail-closed contract as the query itself: abort rather than let
        # `None.date()`/`int(None)` reach the route as an uncontrolled 500.
        if row["created_at"] is None:
            raise ReconciliationAbort(
                f"payments row {row['id']}: created_at is missing"
            )
        if row["loan_id"] is None:
            raise ReconciliationAbort(f"payments row {row['id']}: loan_id is missing")
        created_at = row["created_at"]
        # The window is a property of the job, not of the SQL string.
        if not window_from <= created_at.date() <= window_to:
            continue
        ledger.append(
            LedgerRow(
                payment_id=int(row["id"]),
                loan_id=int(row["loan_id"]),
                amount_minor=_minor_units(
                    row["amount"], source=f"payments row {row['id']}"
                ),
                created_at=created_at,
            )
        )
    return ledger


def _within(
    ledger_row: LedgerRow, settlement_row: SettlementRow, tolerance: timedelta
) -> bool:
    return (
        abs(settlement_row.settlement_date - ledger_row.created_at.date()) <= tolerance
    )


def _rank_candidate(ledger_row, capture, true_window_captures: frozenset):
    """Lower sorts better. Review finding: the matcher used to accept the first
    same-loan/same-amount candidate in date-sort order, so a widened out-of-window
    edge candidate (settlement_date earlier than an in-window exact match) could be
    claimed first, leaving the true in-window row falsely unmatched. Rank instead:
    smallest date gap first (an exact-date match always beats a tolerance match),
    then a true-window candidate over an edge one, then a deterministic tie-break.
    """
    gap_days = abs((capture.settlement_date - ledger_row.created_at.date()).days)
    return (
        gap_days,
        capture not in true_window_captures,
        capture.settlement_date,
        capture.processor_ref,
    )


def _match(
    ledger: list,
    captures: list,
    tolerance_days: int,
    *,
    true_window_ledger: frozenset = frozenset(),
    true_window_captures: frozenset = frozenset(),
):
    """D2(c)/(f). One-to-one; the BEST candidate wins, not the first one scanned.

    A settlement row already matched cannot match a second ledger row. Where counts
    differ on an otherwise identical tuple, the surplus rows on whichever side has
    more are reported unmatched on that side — the job never guesses which of two
    identical rows is the orphan.

    True-window ledger rows are processed before edge ones (an edge row's job is to
    absorb a boundary lag, not to out-compete a true-window row for the best
    candidate), and each row's candidate is chosen by `_rank_candidate` rather than
    "first in sorted order" — the latter made claim order an accident of how
    `ordered_captures` happened to sort, not a decision.

    Exact match always runs first, INCLUDING an exact-amount candidate one day out
    (review finding, rejected): amount equality does not drift, settlement date does,
    which is the only reason a tolerance exists. Demoting an edge exact match below a
    true-window differing-amount one turns a full uncredited capture into a delta and
    drops the displaced edge row out of the report entirely —
    `test_an_edge_exact_match_beats_a_true_window_amount_mismatch`. A second pass
    over what exact match left behind then pairs same-loan rows inside the tolerance
    window whose amounts DIFFER — AMOUNT_MISMATCH — so a rounding/typo discrepancy on
    an otherwise-present payment reports as one mismatch, not a MISSING_IN_SETTLEMENT
    plus an unrelated MISSING_IN_LEDGER that inflates gross_break_minor by both full
    sides instead of the actual delta.
    """
    tolerance = timedelta(days=tolerance_days)
    ordered_ledger = sorted(
        ledger,
        key=lambda r: (
            r.loan_id,
            r.amount_minor,
            r not in true_window_ledger,
            r.created_at,
            r.payment_id,
        ),
    )
    ordered_captures = sorted(
        captures,
        key=lambda r: (r.loan_id, r.amount_minor, r.settlement_date, r.processor_ref),
    )
    claimed = [False] * len(ordered_captures)

    matched_count = 0
    unmatched_ledger = []
    for ledger_row in ordered_ledger:
        candidates = [
            index
            for index, capture in enumerate(ordered_captures)
            if not claimed[index]
            and capture.loan_id == ledger_row.loan_id
            and capture.amount_minor == ledger_row.amount_minor
            and _within(ledger_row, capture, tolerance)
        ]
        if not candidates:
            unmatched_ledger.append(ledger_row)
            continue
        best_index = min(
            candidates,
            key=lambda index: _rank_candidate(
                ledger_row, ordered_captures[index], true_window_captures
            ),
        )
        claimed[best_index] = True
        matched_count += 1

    unmatched_captures = [
        c for index, c in enumerate(ordered_captures) if not claimed[index]
    ]

    unmatched_ledger.sort(
        key=lambda r: (
            r.loan_id,
            r not in true_window_ledger,
            r.created_at,
            r.payment_id,
        )
    )
    unmatched_captures.sort(
        key=lambda r: (r.loan_id, r.settlement_date, r.processor_ref)
    )
    mismatch_claimed = [False] * len(unmatched_captures)
    mismatches = []
    remaining_ledger = []
    for ledger_row in unmatched_ledger:
        candidates = [
            index
            for index, capture in enumerate(unmatched_captures)
            if not mismatch_claimed[index]
            and capture.loan_id == ledger_row.loan_id
            and _within(ledger_row, capture, tolerance)
        ]
        if not candidates:
            remaining_ledger.append(ledger_row)
            continue
        best_index = min(
            candidates,
            key=lambda index: _rank_candidate(
                ledger_row, unmatched_captures[index], true_window_captures
            ),
        )
        mismatch_claimed[best_index] = True
        mismatches.append((ledger_row, unmatched_captures[best_index]))

    remaining_captures = [
        c for index, c in enumerate(unmatched_captures) if not mismatch_claimed[index]
    ]
    return matched_count, remaining_ledger, remaining_captures, mismatches


def _duplicate_suspect_window_seconds() -> int:
    """D2(e). No default: a guessed bound is worse than no detection at all.

    The 30-day ceiling (config.MAX_DUPLICATE_SUSPECT_WINDOW_SECONDS, imported rather
    than redeclared here) rejects a misconfigured operator value — a stray extra
    digit, a units mix-up — before `timedelta(seconds=...)` in
    `_find_duplicate_suspects` can raise `OverflowError` on a pathological one. The
    same ceiling gates config.duplicate_suspect_window_configured(), so a value this
    function would abort on already fails /health instead of only /reconciliation/peek.
    """
    raw = (DUPLICATE_SUSPECT_WINDOW_SECONDS or "").strip()
    if not raw:
        raise ReconciliationAbort(
            "DUPLICATE_SUSPECT_WINDOW_SECONDS is not set; duplicate detection cannot "
            "run without a bound"
        )
    try:
        seconds = int(raw)
    except ValueError:
        raise ReconciliationAbort(
            f"DUPLICATE_SUSPECT_WINDOW_SECONDS={raw!r} is not an integer"
        )
    if seconds > MAX_DUPLICATE_SUSPECT_WINDOW_SECONDS:
        raise ReconciliationAbort(
            f"DUPLICATE_SUSPECT_WINDOW_SECONDS={seconds} exceeds the sane ceiling of "
            f"{MAX_DUPLICATE_SUSPECT_WINDOW_SECONDS} (30 days)"
        )
    if seconds <= 0:
        raise ReconciliationAbort(
            f"DUPLICATE_SUSPECT_WINDOW_SECONDS={seconds} must be positive"
        )
    return seconds


def _find_duplicate_suspects(
    ledger: list, window_seconds: int, true_window_payment_ids: frozenset
) -> list:
    """D2(e). Ledger side only — never consults settlement, so no tolerance can hide it.

    Rows sharing `(loan_id, amount_minor)` are ordered by `created_at`, and every
    adjacent pair inside `window_seconds` becomes a candidate. Candidates are then
    claimed at most once per row, so three same-amount rows in a row report one pair
    and one clean row rather than two overlapping pairs double-counting the middle
    one.

    Candidates that touch the true window are claimed FIRST (review finding). Claiming
    left-to-right instead let a pair sitting wholly before `window_from` consume the
    row that the window's own retry needed to pair with: the outside pair was then
    dropped by the caller's relevance filter and the in-window retry was reported by
    no window at all — the previous window's run reports the outside pair and treats
    the third row as clean. Ordering by relevance before claiming costs nothing when
    every row is in the window, where the created_at tie-break leaves the original
    left-to-right result unchanged.
    """
    groups: dict = {}
    for row in ledger:
        groups.setdefault((row.loan_id, row.amount_minor), []).append(row)

    gap = timedelta(seconds=window_seconds)
    suspects = []
    for (loan_id, amount_minor), rows in groups.items():
        ordered = sorted(rows, key=lambda r: (r.created_at, r.payment_id))
        candidates = []
        for index in range(len(ordered) - 1):
            current, following = ordered[index], ordered[index + 1]
            delta = following.created_at - current.created_at
            if delta <= gap:
                candidates.append((current, following, delta))
        candidates.sort(
            key=lambda pair: (
                not (
                    pair[0].payment_id in true_window_payment_ids
                    or pair[1].payment_id in true_window_payment_ids
                ),
                pair[0].created_at,
                pair[0].payment_id,
            )
        )
        claimed: set = set()
        for current, following, delta in candidates:
            if current.payment_id in claimed or following.payment_id in claimed:
                continue
            claimed.add(current.payment_id)
            claimed.add(following.payment_id)
            suspects.append(
                DuplicateSuspect(
                    loan_id=loan_id,
                    amount_minor=amount_minor,
                    occurred_on=current.created_at.date(),
                    gap_seconds=int(delta.total_seconds()),
                    first_payment_id=current.payment_id,
                    second_payment_id=following.payment_id,
                )
            )

    suspects.sort(key=lambda s: (s.loan_id, s.amount_minor, s.occurred_on))
    return suspects


def reconcile(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    tolerance_days: int = MATCH_TOLERANCE_DAYS,
    settlement_path: Optional[str] = None,
) -> ReconciliationResult:
    """Reconcile the capture side against the settlement file over a date window.

    Raises `ReconciliationAbort` (exit 2) rather than returning a result it could not
    verify. Returns a result whose `exit_code` is 0 (clean) or 1 (findings).
    """
    path = settlement_path or SETTLEMENT_FILE

    if tolerance_days < 0:
        raise ReconciliationAbort(
            f"tolerance_days must not be negative: {tolerance_days}"
        )

    all_settlement = load_settlement(path)

    # D2(a). Default window is the settlement file's own range, and it applies to both
    # sides identically for TOTALS: `ledger`/`settlement` below, and everything derived
    # from them (refunds, net/per-loan/gross variance), are cut to exactly
    # window_from..window_to. Without this the comparison is the P1 defect with more
    # steps (ledger_total() summed the whole payments table against a settlement file
    # covering days).
    window_from = from_date or min(r.settlement_date for r in all_settlement)
    window_to = to_date or max(r.settlement_date for r in all_settlement)
    if window_from > window_to:
        raise ReconciliationAbort(
            f"window start {window_from} is after window end {window_to}"
        )

    # D2(c) boundary fix. A capture the day before window_from that settles on
    # window_from (or a ledger row on window_to settling the day after) is exactly the
    # settlement lag the ±1 day tolerance exists to absorb — but a candidate pool
    # pre-cut to the exact reporting window never gives it the chance: `_match` cannot
    # pair a row it was never handed. The MATCHING candidate pool is widened by
    # tolerance_days on each edge; TOTALS above stay on the narrow, true window, so
    # this cannot reintroduce the P1 defect (a wider match, not a wider total).
    candidate_from = window_from - timedelta(days=tolerance_days)
    candidate_to = window_to + timedelta(days=tolerance_days)

    settlement = [
        r for r in all_settlement if window_from <= r.settlement_date <= window_to
    ]
    settlement_candidates = [
        r for r in all_settlement if candidate_from <= r.settlement_date <= candidate_to
    ]

    ledger_candidates = load_ledger(candidate_from, candidate_to)
    ledger = [
        r for r in ledger_candidates if window_from <= r.created_at.date() <= window_to
    ]

    # D2(e) boundary fix (review finding). A retry pair split across the window edge
    # -- e.g. 23:59:59 the day before window_from and 00:00:01 on window_from -- is
    # within DUPLICATE_SUSPECT_WINDOW_SECONDS of each other, but the narrow
    # true-window `ledger` above only ever holds one side of it, so a scan over
    # `ledger` alone never sees the pair. The candidate pool for THIS scan is
    # widened separately, by the duplicate window itself, not by tolerance_days --
    # they are unrelated configuration values. `date` arithmetic ignores the
    # sub-day part of a `timedelta` (`date - timedelta(seconds=90)` is a no-op), so
    # the margin is computed in whole days, generous by construction: the exact
    # datetime gap comparison still happens inside `_find_duplicate_suspects`, so
    # over-fetching by up to a day cannot manufacture a false pair, only avoid
    # missing a true one that a day-granularity fetch would otherwise cut off.
    duplicate_window_seconds = _duplicate_suspect_window_seconds()
    duplicate_margin_days = duplicate_window_seconds // 86400 + 1
    duplicate_candidates = load_ledger(
        window_from - timedelta(days=duplicate_margin_days),
        window_to + timedelta(days=duplicate_margin_days),
    )
    # A pair entirely outside the true window is not this window's signal to
    # report -- it belongs to whichever window actually contains it.
    true_window_payment_ids = frozenset(row.payment_id for row in ledger)
    duplicates = [
        suspect
        for suspect in _find_duplicate_suspects(
            duplicate_candidates, duplicate_window_seconds, true_window_payment_ids
        )
        if suspect.first_payment_id in true_window_payment_ids
        or suspect.second_payment_id in true_window_payment_ids
    ]

    capture_candidates = [r for r in settlement_candidates if r.row_type == CAPTURE]
    refunds = [r for r in settlement if r.row_type == REFUND]
    # Review finding: a widened edge candidate must never out-compete a true-window
    # row for the best match. `_match` uses these sets to rank exact/true-window
    # candidates ahead of tolerance/edge ones instead of accepting whichever
    # candidate happened to sort first.
    true_window_ledger = frozenset(ledger)
    true_window_capture_rows = [r for r in settlement if r.row_type == CAPTURE]
    true_window_captures = frozenset(true_window_capture_rows)
    matched_count, unmatched_ledger, unmatched_captures, mismatches = _match(
        ledger_candidates,
        capture_candidates,
        tolerance_days,
        true_window_ledger=true_window_ledger,
        true_window_captures=true_window_captures,
    )
    breaks: list = []

    # A row the widened candidate pool pulled in from outside the true window that
    # still failed to match is not this window's break to report — it is either out
    # of scope entirely or belongs to whichever window actually contains it.
    breaks.extend(
        Break(
            break_class=MISSING_IN_SETTLEMENT,
            loan_id=row.loan_id,
            amount_minor=row.amount_minor,
            occurred_on=row.created_at.date(),
            payment_id=row.payment_id,
            detail="credited, never captured",
        )
        for row in unmatched_ledger
        if window_from <= row.created_at.date() <= window_to
    )
    breaks.extend(
        Break(
            break_class=MISSING_IN_LEDGER,
            loan_id=row.loan_id,
            amount_minor=row.amount_minor,
            occurred_on=row.settlement_date,
            processor_ref=row.processor_ref,
            detail="captured, never credited",
        )
        for row in unmatched_captures
        if window_from <= row.settlement_date <= window_to
    )
    # A refund is a schema limitation, not a lost payment: `payments` has no direction
    # column, so it cannot hold a counterpart. Its own class, deliberately, so a
    # known-benign row does not sit next to customer money that went missing.
    breaks.extend(
        Break(
            break_class=REFUND_UNREPRESENTED,
            loan_id=row.loan_id,
            amount_minor=row.amount_minor,
            occurred_on=row.settlement_date,
            processor_ref=row.processor_ref,
            detail="settlement refund the payments table cannot represent",
        )
        for row in refunds
    )
    # D2(f). The true discrepancy is the delta, not either side's full amount — a
    # ledger row for 10000 against a settled capture for 9999 is a 1-minor-unit
    # mismatch, not 10000 missing plus 9999 unaccounted for.
    breaks.extend(
        Break(
            break_class=AMOUNT_MISMATCH,
            loan_id=ledger_row.loan_id,
            amount_minor=abs(ledger_row.amount_minor - capture.amount_minor),
            occurred_on=ledger_row.created_at.date(),
            processor_ref=capture.processor_ref,
            payment_id=ledger_row.payment_id,
            detail=(
                f"ledger {ledger_row.amount_minor} minor units vs settlement "
                f"{capture.amount_minor} minor units ({capture.processor_ref}, "
                f"{capture.settlement_date.isoformat()})"
            ),
        )
        for ledger_row, capture in mismatches
        if (
            window_from <= ledger_row.created_at.date() <= window_to
            or window_from <= capture.settlement_date <= window_to
        )
    )
    breaks.sort(key=lambda b: (b.break_class, b.loan_id, b.occurred_on, b.amount_minor))

    ledger_by_loan: dict = {}
    for row in ledger:
        ledger_by_loan[row.loan_id] = (
            ledger_by_loan.get(row.loan_id, 0) + row.amount_minor
        )
    settlement_by_loan: dict = {}
    for row in settlement:
        signed = row.amount_minor if row.row_type == CAPTURE else -row.amount_minor
        settlement_by_loan[row.loan_id] = (
            settlement_by_loan.get(row.loan_id, 0) + signed
        )

    net_variance_minor = sum(ledger_by_loan.values()) - sum(settlement_by_loan.values())
    per_loan_absolute_minor = sum(
        abs(ledger_by_loan.get(loan_id, 0) - settlement_by_loan.get(loan_id, 0))
        for loan_id in set(ledger_by_loan) | set(settlement_by_loan)
    )

    return ReconciliationResult(
        window_from=window_from,
        window_to=window_to,
        tolerance_days=tolerance_days,
        matched_count=matched_count,
        breaks=breaks,
        duplicates=duplicates,
        net_variance_minor=net_variance_minor,
        per_loan_absolute_minor=per_loan_absolute_minor,
        gross_break_minor=sum(b.amount_minor for b in breaks),
        ledger_row_count=len(ledger),
        ledger_total_minor=sum(ledger_by_loan.values()),
        settlement_row_count=len(settlement),
        settlement_captures_minor=sum(r.amount_minor for r in true_window_capture_rows),
        settlement_refunds_minor=sum(r.amount_minor for r in refunds),
    )


def _break_json(item: Break) -> dict:
    """One break as JSON. Optional keys appear only when they are known.

    Breaks only — a duplicate suspect goes through `_duplicate_json`; the two carry
    different fields and neither dataclass holds the other's. `gap_seconds` was read
    here while the two shared a type, and `Break` has never had it.

    `pan` and `cvv` are never read, so they cannot reach this document; the report is
    stdout and is not redactor-covered, which is why the column list is a review point
    rather than an assumption (spec's redaction note).
    """
    document = {
        "class": item.break_class,
        "loan_id": item.loan_id,
        "amount_minor": item.amount_minor,
        "date": item.occurred_on.isoformat(),
    }
    for key, value in (
        ("processor_ref", item.processor_ref),
        ("payment_id", item.payment_id),
        ("detail", item.detail or None),
    ):
        if value is not None:
            document[key] = value
    return document


def _duplicate_json(item: DuplicateSuspect) -> dict:
    """One duplicate suspect as JSON. A `DuplicateSuspect` is NOT a `Break`.

    It has no `break_class` and no single `payment_id` — it names a PAIR, and the two
    ids are the whole remediation instruction: they are what an operator refunds or
    voids. Rendering it through `_break_json` raised
    `AttributeError: 'DuplicateSuspect' object has no attribute 'break_class'` on any
    run that found one, which the seeded sample always does (loan 5582), so both the
    CLI and `peek` traceback rather than report. `class` is stamped here because the
    dataclass does not carry one — the document's reader keys off it exactly as it does
    for a break.
    """
    return {
        "class": DUPLICATE_SUSPECT,
        "loan_id": item.loan_id,
        "amount_minor": item.amount_minor,
        "date": item.occurred_on.isoformat(),
        "gap_seconds": item.gap_seconds,
        "first_payment_id": item.first_payment_id,
        "second_payment_id": item.second_payment_id,
    }


def build_report(result: ReconciliationResult) -> dict:
    """The D3(b) break report — ONE document shape, for the CLI and for `peek`.

    Both callers render this, so there is no second, weaker comparison to drift away
    from the real one. That drift is what the fee-schedule loader was built to end and
    why `ledger_total()`/`settlement_total()` were deleted rather than left in place.

    Each figure carries what it depends on (D3(c)). Reporting only the net variance is
    the reporting failure behind "month-end is a little noisy" — on this sample -88882
    against 175318, so the netting hides roughly half the error. Reporting the gross
    break value without saying it moves with the tolerance would be the same mistake
    again: a figure that looks like a measurement but is partly an artifact of a
    constant we chose. The two coincide here at +/-1 day; that is a coincidence of this
    data, not a property.
    """
    return {
        "window": {
            "from": result.window_from.isoformat(),
            "to": result.window_to.isoformat(),
            "tolerance_days": result.tolerance_days,
        },
        "ledger": {
            "rows": result.ledger_row_count,
            "total_minor": result.ledger_total_minor,
        },
        "settlement": {
            "rows": result.settlement_row_count,
            "captures_minor": result.settlement_captures_minor,
            "refunds_minor": result.settlement_refunds_minor,
            "net_minor": result.settlement_net_minor,
        },
        "matched": result.matched_count,
        "figures": {
            "net_variance_minor": {
                "value": result.net_variance_minor,
                "depends_on_matching_tolerance": False,
            },
            "per_loan_absolute_variance_minor": {
                "value": result.per_loan_absolute_minor,
                "depends_on_matching_tolerance": False,
            },
            "gross_break_value_minor": {
                "value": result.gross_break_minor,
                "depends_on_matching_tolerance": True,
            },
        },
        "breaks": [_break_json(b) for b in result.breaks],
        # Separate from `breaks` on purpose: a duplicate is a signal, not a variance,
        # and adding it to one would double-count money already counted on one side.
        "duplicate_suspects": [_duplicate_json(d) for d in result.duplicates],
        "exit_code": result.exit_code,
    }
