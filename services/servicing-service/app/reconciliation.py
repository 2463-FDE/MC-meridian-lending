"""Reconciliation — reading the processor settlement file (debt D7, in progress).

Spec: `docs/spec-observability-week7.md` §D2(b) and §D2(g). ADR 0015 holds the decisions.

This is the read half. `settlement_total()` used to return `0.0` when the file was
absent — no exception, no signal, a number reported over a file it never read — and it
summed binary floats to answer a question about whether two figures tie out. Both are
now gone: the file is parsed into typed rows carrying **integer minor units**, and any
condition that stops the read from happening raises `ReconciliationAbort` instead of
producing a total.

Still true after this change, and addressed next: the two totals are not comparable.
`ledger_total()` spans the entire `payments` table while the settlement file covers
three loans over seven days, so the subtraction remains meaningless until the row-level
comparison replaces it. That work deletes both helpers.
"""

import csv
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from . import db
from .config import SETTLEMENT_FILE

# D2(g), mirroring `scripts/prove_test.sh`'s convention. "Could not check" is never
# reported as 0, and never as 1 either: a zero-break result from a run that read
# nothing is the failure this control exists to prevent. The comparison that returns
# 0 and 1 lands with the matcher; the codes are fixed here because the abort is.
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


def ledger_total() -> float:
    rows = db.query("SELECT COALESCE(SUM(amount), 0) AS total FROM payments")
    return float(rows[0]["total"]) if rows else 0.0


def settlement_total() -> float:
    """Net settlement across the whole file, captures less refunds.

    Same meaning as before; only the failure mode changed. It reads through
    `load_settlement`, so an unreadable file now aborts rather than reporting `0.0`.

    The netting is done in minor units and converted once at the boundary, because
    this helper's callers still expect a float. That conversion is why it is a step and
    not a destination: the comparison it feeds is replaced by the row-level matcher,
    which stays in minor units end to end and deletes this.
    """
    rows = load_settlement(SETTLEMENT_FILE)
    net_minor = sum(
        row.amount_minor if row.row_type == CAPTURE else -row.amount_minor
        for row in rows
    )
    return net_minor / 100


# NOTE: nothing calls these on a schedule. No break-report. No alert. (D7)
