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

The ±1 day tolerance means a same-day double charge matches two different settled
captures, so matching alone reports it clean. `find_duplicate_suspects` therefore reads
the ledger side ONLY, before matching, so no window tolerance can hide it (D2(d)/(e)).
"""

import csv
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from . import db
from .config import SETTLEMENT_FILE, duplicate_suspect_window_seconds

# D2(c). The ledger stamps `created_at` at capture and the processor stamps
# `settlement_date` at settlement; no cut-off convention has been confirmed by the
# client (spec Client Questions Q5). Named, not buried in a comparison.
MATCH_TOLERANCE_DAYS = 1

# D2(f). Every unmatched row lands in exactly one of these.
MISSING_IN_LEDGER = "MISSING_IN_LEDGER"
MISSING_IN_SETTLEMENT = "MISSING_IN_SETTLEMENT"
REFUND_UNREPRESENTED = "REFUND_UNREPRESENTED"
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
    """One finding. `amount_minor` is the value at stake, always integer cents.

    For `DUPLICATE_SUSPECT` it is the amount charged twice, which is a *signal* and is
    not added to any variance figure — the duplicate's money is already accounted for
    on whichever side it landed, so counting it again would double-count it.
    """

    break_class: str
    loan_id: int
    amount_minor: int
    occurred_on: date
    processor_ref: Optional[str] = None
    payment_id: Optional[int] = None
    gap_seconds: Optional[int] = None
    detail: str = ""


@dataclass(frozen=True)
class ReconciliationResult:
    window_from: date
    window_to: date
    tolerance_days: int
    matched_count: int
    breaks: list = field(default_factory=list)
    duplicates: list = field(default_factory=list)
    # D2(b): every figure is an int of minor units.
    net_variance_minor: int = 0
    per_loan_absolute_minor: int = 0
    gross_break_minor: int = 0

    @property
    def exit_code(self) -> int:
        """1 when there is anything to answer for, including a duplicate alone.

        A `DUPLICATE_SUSPECT` is not a variance, but exiting 0 — "reconciled, no
        breaks" — over a detected double charge would report clean on exactly the
        defect this control exists to surface.
        """
        if self.breaks or self.duplicates:
            return EXIT_BREAKS
        return EXIT_CLEAN


def load_ledger(window_from: date, window_to: date) -> list:
    """The capture side, inside the window. SELECT only; `pan`/`cvv` are never read."""
    rows = db.query(
        "SELECT id, loan_id, amount, created_at "
        "FROM payments "
        "WHERE created_at >= %s AND created_at < %s "
        "ORDER BY loan_id, amount, created_at, id",
        (window_from, window_to + timedelta(days=1)),
    )
    ledger = []
    for row in rows:
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


def find_duplicate_suspects(ledger: list, gap_seconds: int) -> list:
    """D2(e). Scans the ledger side alone, before and independently of matching.

    The ±1 day tolerance makes a same-day double charge match two different settled
    captures, so matching reports it clean (D2(d)). This scan does not consult the
    settlement side at all, which is why no window tolerance can hide it.

    The bound is in minutes, not days, on purpose: `disclosure-service`'s
    `schedule.py::_add_months` generates one due date per calendar month, so two
    legitimate equal-amount rows on one loan are at least a billing cycle apart. A
    minutes-scale bound catches the retry/replay case without touching a normal
    monthly recurrence.
    """
    grouped: dict = {}
    for row in ledger:
        grouped.setdefault((row.loan_id, row.amount_minor), []).append(row)

    duplicates = []
    for (loan_id, amount_minor), rows in sorted(grouped.items()):
        ordered = sorted(rows, key=lambda r: (r.created_at, r.payment_id))
        for earlier, later in zip(ordered, ordered[1:]):
            gap = int((later.created_at - earlier.created_at).total_seconds())
            if gap <= gap_seconds:
                duplicates.append(
                    Break(
                        break_class=DUPLICATE_SUSPECT,
                        loan_id=loan_id,
                        amount_minor=amount_minor,
                        occurred_on=later.created_at.date(),
                        payment_id=later.payment_id,
                        gap_seconds=gap,
                        detail=(
                            f"payments {earlier.payment_id} and {later.payment_id} "
                            f"{gap}s apart"
                        ),
                    )
                )
    return duplicates




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

    # Fail closed before reading anything: the duplicate scan is not optional, so an
    # unconfigured bound is an abort, not a run with the scan quietly skipped.
    gap_seconds = duplicate_suspect_window_seconds()
    if gap_seconds is None:
        raise ReconciliationAbort(
            "DUPLICATE_SUSPECT_WINDOW_SECONDS is not configured (positive integer "
            "seconds); refusing to run duplicate detection against a guessed bound"
        )
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

    duplicates = find_duplicate_suspects(ledger, gap_seconds)

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
