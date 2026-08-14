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
from .config import DUPLICATE_SUSPECT_WINDOW_SECONDS, SETTLEMENT_FILE

# D2(c). The ledger stamps `created_at` at capture and the processor stamps
# `settlement_date` at settlement; no cut-off convention has been confirmed by the
# client (spec Client Questions Q5). Named, not buried in a comparison.
MATCH_TOLERANCE_DAYS = 1

# D2(f). Every unmatched row lands in exactly one of these.
MISSING_IN_LEDGER = "MISSING_IN_LEDGER"
MISSING_IN_SETTLEMENT = "MISSING_IN_SETTLEMENT"
REFUND_UNREPRESENTED = "REFUND_UNREPRESENTED"

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

    @property
    def exit_code(self) -> int:
        if self.breaks:
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


def _match(ledger: list, captures: list, tolerance_days: int):
    """D2(c). One-to-one and greedy in `(loan_id, amount_minor, date)` order.

    A settlement row already matched cannot match a second ledger row. Where counts
    differ on an otherwise identical tuple, the surplus rows on whichever side has
    more are reported unmatched on that side — the job never guesses which of two
    identical rows is the orphan.
    """
    tolerance = timedelta(days=tolerance_days)
    ordered_ledger = sorted(
        ledger, key=lambda r: (r.loan_id, r.amount_minor, r.created_at, r.payment_id)
    )
    ordered_captures = sorted(
        captures,
        key=lambda r: (r.loan_id, r.amount_minor, r.settlement_date, r.processor_ref),
    )
    claimed = [False] * len(ordered_captures)

    matched_count = 0
    unmatched_ledger = []
    for ledger_row in ordered_ledger:
        for index, capture in enumerate(ordered_captures):
            if claimed[index]:
                continue
            if (
                capture.loan_id == ledger_row.loan_id
                and capture.amount_minor == ledger_row.amount_minor
                and _within(ledger_row, capture, tolerance)
            ):
                claimed[index] = True
                matched_count += 1
                break
        else:
            unmatched_ledger.append(ledger_row)

    unmatched_captures = [
        c for index, c in enumerate(ordered_captures) if not claimed[index]
    ]
    return matched_count, unmatched_ledger, unmatched_captures


# D2(e) says the bound is "scoped in minutes, not days". 30 days is far beyond that
# intent but still a sane ceiling: it rejects a misconfigured operator value (a stray
# extra digit, a units mix-up) before `timedelta(seconds=...)` in
# `_find_duplicate_suspects` can raise `OverflowError` on a pathological one.
_MAX_DUPLICATE_SUSPECT_WINDOW_SECONDS = 30 * 24 * 60 * 60


def _duplicate_suspect_window_seconds() -> int:
    """D2(e). No default: a guessed bound is worse than no detection at all."""
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
    if seconds > _MAX_DUPLICATE_SUSPECT_WINDOW_SECONDS:
        raise ReconciliationAbort(
            f"DUPLICATE_SUSPECT_WINDOW_SECONDS={seconds} exceeds the sane ceiling of "
            f"{_MAX_DUPLICATE_SUSPECT_WINDOW_SECONDS} (30 days)"
        )
    if seconds <= 0:
        raise ReconciliationAbort(
            f"DUPLICATE_SUSPECT_WINDOW_SECONDS={seconds} must be positive"
        )
    return seconds


def _find_duplicate_suspects(ledger: list, window_seconds: int) -> list:
    """D2(e). Ledger side only — never consults settlement, so no tolerance can hide it.

    Rows sharing `(loan_id, amount_minor)` are ordered by `created_at` and paired
    adjacently: once a row is claimed by a pair it cannot pair again, so three
    same-amount rows in a row report one pair and one clean row, not two overlapping
    pairs double-counting the middle row.
    """
    groups: dict = {}
    for row in ledger:
        groups.setdefault((row.loan_id, row.amount_minor), []).append(row)

    gap = timedelta(seconds=window_seconds)
    suspects = []
    for (loan_id, amount_minor), rows in groups.items():
        ordered = sorted(rows, key=lambda r: (r.created_at, r.payment_id))
        index = 0
        while index < len(ordered) - 1:
            current, following = ordered[index], ordered[index + 1]
            delta = following.created_at - current.created_at
            if delta <= gap:
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
                index += 2
            else:
                index += 1

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

    settlement = load_settlement(path)

    # D2(a). Default window is the settlement file's own range, and it applies to both
    # sides identically. Known boundary property, stated rather than worked around: a
    # capture on the day before `from_date` that settles on `from_date` is out of scope,
    # so its settlement row reports MISSING_IN_LEDGER. The tolerance operates inside the
    # window, not across its edge. Widening the ledger fetch by the tolerance instead
    # would make the two sides span different periods — the P1 defect at the edge. The
    # caller controls this by choosing the window (a calendar month, not the file's exact
    # min/max).
    window_from = from_date or min(r.settlement_date for r in settlement)
    window_to = to_date or max(r.settlement_date for r in settlement)
    if window_from > window_to:
        raise ReconciliationAbort(
            f"window start {window_from} is after window end {window_to}"
        )

    settlement = [
        r for r in settlement if window_from <= r.settlement_date <= window_to
    ]
    ledger = load_ledger(window_from, window_to)

    # D2(e). Independent of matching and of `tolerance_days`: computed from the ledger
    # alone, before `_match` runs, so a same-day double charge cannot hide behind the
    # settlement-lag tolerance the way it does in `_match`'s output (D2(d)).
    duplicates = _find_duplicate_suspects(ledger, _duplicate_suspect_window_seconds())

    captures = [r for r in settlement if r.row_type == CAPTURE]
    refunds = [r for r in settlement if r.row_type == REFUND]
    matched_count, unmatched_ledger, unmatched_captures = _match(
        ledger, captures, tolerance_days
    )
    breaks: list = []

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
    )
