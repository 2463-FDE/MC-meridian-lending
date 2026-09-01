"""Balance + payment application.

Money is float. The balance is a single column overwritten in place (no ledger,
no transaction history).

`apply_payment` is atomic as of D3 (ADR 0020): it records the application and moves
the balance in one statement, computing from the stored value inside the UPDATE.
`waive_fee` is atomic the same way as of D32's first half: the decrement computes
from the stored `past_due` inside its own UPDATE. `adjust_balance` closes D32's
second half via compare-and-set: it sets an absolute figure rather than a delta, so
the decrement shape doesn't apply to it — it refuses instead when the stored balance
no longer matches what the caller was quoted (D32, docs/debt-log.md). (D2, D12, D14
still remain — no ledger entry, still float, still no waterfall.)
"""

from .logging_config import get_logger
from . import db

log = get_logger("balance")


class PaymentNotApplicable(Exception):
    """This payment cannot be applied, and no money moved.

    Carries a machine-readable `reason` so the route can say which of the refusals
    it was without the caller parsing prose. Every reason means the same thing to
    the balance: nothing was written.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class BalanceChanged(Exception):
    """`adjust_balance`'s compare-and-set refused: the stored balance no longer
    matches what the caller quoted, so nothing was written. Carries the current
    balance so the caller can show it and require a fresh, deliberate resubmit —
    never an automatic retry against a figure the operator never saw."""

    def __init__(self, current_balance: float):
        super().__init__("balance changed since it was quoted")
        self.current_balance = current_balance


def get_balance(loan_id: int) -> float:
    rows = db.query("SELECT balance FROM balances WHERE loan_id = %s", (loan_id,))
    return rows[0]["balance"] if rows else 0.0


def get_past_due(loan_id: int) -> float:
    rows = db.query("SELECT past_due FROM balances WHERE loan_id = %s", (loan_id,))
    return rows[0]["past_due"] if rows else 0.0


# The whole of D3. One statement, so the record and the movement commit or roll back
# together on an autocommit connection without this module opening a transaction on the
# process-wide connection `db.py` hands every thread.
#
# Four properties, none optional (docs/specs/payments-week5.md D3(d)):
#
# 1. The UPDATE computes from the stored value INSIDE the statement. Under READ COMMITTED
#    a concurrent updater blocks on the row lock and then re-evaluates `b.balance` against
#    the committed row, so the two applies serialize instead of both reading one opening
#    figure and the last writer winning. This is the fix for the $200.
# 2. Nothing the caller sends is a source of truth. The loan credited and the amount
#    credited both come out of the `payments` row; `loan_id` is a predicate the SELECT has
#    to match. Without this an internal or reaper call credits the wrong loan, and
#    UNIQUE (payment_id) then makes it permanent AND blocks the correct retry.
# 3. The JOIN to `balances` is what makes "this loan has no balances row" produce zero
#    eligible rows rather than an application row recording an apply that moved no money.
#    Spec D3(d) reaches the same place with an explicit ROLLBACK; the join reaches it
#    without needing a transaction, which matters because `db.get_conn` is one shared
#    autocommit connection and flipping its autocommit off would span other threads' work.
# 4. ON CONFLICT (payment_id) DO NOTHING makes a replay move nothing. Zero returned rows
#    is therefore two different facts, and _already_applied below tells them apart.
#
# `status IN ('captured','settled')` is also what makes an ACH `submitted` payment move no
# balance a property of the write rather than of the caller's discipline. Both charge
# handlers now finalize the row to `captured` BEFORE calling this (D19 wrote the status
# after the apply, which would make this predicate refuse every live payment); a row still
# `processing` is a capture nobody has confirmed and must not credit.
_APPLY_SQL = """
WITH eligible AS (
    SELECT p.loan_id, p.id AS payment_id, p.amount_minor
      FROM payments p
      JOIN balances b ON b.loan_id = p.loan_id
     WHERE p.id = %(payment_id)s
       AND p.loan_id = %(loan_id)s
       AND p.amount_minor IS NOT NULL
       AND p.status IN ('captured', 'settled')
), ins AS (
    INSERT INTO payment_applications (loan_id, payment_id, amount_minor)
    SELECT loan_id, payment_id, amount_minor FROM eligible
    ON CONFLICT (payment_id) DO NOTHING
    RETURNING loan_id, amount_minor
)
UPDATE balances b
   SET balance = b.balance - (ins.amount_minor / 100.0),
       updated_at = now()
  FROM ins
 WHERE b.loan_id = ins.loan_id
RETURNING b.loan_id, b.balance, ins.amount_minor
"""

# Read back by payment_id, NOT by (payment_id, loan_id): the row's own loan is what the
# caller's claim gets checked against below. Selecting on both would answer "no prior
# application" for a payment already applied to a DIFFERENT loan, and the refusal that
# case needs would be reported as an ordinary ineligibility.
_ALREADY_APPLIED_SQL = (
    "SELECT loan_id, amount_minor FROM payment_applications WHERE payment_id = %s"
)


def apply_payment(
    loan_id: int, payment_id: int, request_id: str = "-"
) -> tuple[float, bool]:
    """Apply a captured payment to its loan's balance, exactly once.

    Returns `(balance, moved)` — the balance after this call, and whether THIS call is
    the one that moved it. A replay returns `(current_balance, False)`: the money is
    already on the loan, so the caller has succeeded, but it did not credit anything.

    Raises `PaymentNotApplicable` when nothing was written, which is every other case:
    no such payment, a payment belonging to a different loan, `amount_minor` still NULL
    (a pre-0018 row on a volume missing migration 0019's backfill), a status that does
    not credit, or a loan with no `balances` row.

    Both callers (main.apply_payment's route handler and payments.charge) pass the span
    fields down, so this line joins the same request_id-scoped log search as theirs
    (docs/specs/observability-week7.md P2).
    """
    rows = db.query(_APPLY_SQL, {"payment_id": payment_id, "loan_id": loan_id})
    if rows:
        row = rows[0]
        new_balance = row["balance"]
        # Same four span fields (request_id/loan_id/payment_id/outcome) in the same order
        # the callers use, logged AFTER the balance actually moves, so this mutation is not
        # a hole in the correlated span.
        log.info(
            "applied payment request_id=%s loan_id=%s payment_id=%s outcome=%s "
            "amount_minor=%s new_balance=%s",
            request_id,
            loan_id,
            payment_id,
            "applied",
            row["amount_minor"],
            new_balance,
        )
        return float(new_balance), True

    # Zero rows. Either this payment was already applied, or it was never eligible.
    prior = db.query(_ALREADY_APPLIED_SQL, (payment_id,))
    if not prior:
        raise PaymentNotApplicable("not_applicable")
    if prior[0]["loan_id"] != loan_id:
        # The payment is applied, to someone else's loan. Answering "already applied" here
        # would let a caller read this as success for THIS loan.
        raise PaymentNotApplicable("applied_to_another_loan")
    current = get_balance(loan_id)
    log.info(
        "applied payment request_id=%s loan_id=%s payment_id=%s outcome=%s "
        "amount_minor=%s new_balance=%s",
        request_id,
        loan_id,
        payment_id,
        "already_applied",
        prior[0]["amount_minor"],
        current,
    )
    return float(current), False


def adjust_balance(loan_id: int, new_value: float, expected_balance: float) -> float:
    """Set the balance to `new_value`, but only if the stored balance still equals
    `expected_balance` — the figure the operator was quoted when they opened the
    correction. One statement: the predicate and the write commit together, so a
    concurrent `apply_payment`/`waive_fee`/`adjust_balance` between quote and submit
    is caught rather than silently overwritten (D32 second half).

    Raises `BalanceChanged` (nothing written) when the row moved underneath the
    caller. No ledger entry on success either way; the prior value is still gone.
    """
    rows = db.query(
        "UPDATE balances SET balance = %s, updated_at = now() "
        "WHERE loan_id = %s AND balance = %s RETURNING balance",
        (float(new_value), loan_id, float(expected_balance)),
    )
    if rows:
        log.info(
            "adjusted balance loan_id=%s %s -> %s",
            loan_id,
            expected_balance,
            new_value,
        )
        return float(rows[0]["balance"])
    current = get_balance(loan_id)
    log.warning(
        "adjust_balance refused loan_id=%s expected=%s actual=%s",
        loan_id,
        expected_balance,
        current,
    )
    raise BalanceChanged(current)


def waive_fee(loan_id: int, amount: float) -> float:
    """Reduce past_due by amount, atomically (D32 first half).

    One statement: the decrement computes from the stored `past_due` inside the
    UPDATE, so two concurrent waives on the same loan serialize on the row lock
    instead of both reading one opening figure and the last writer winning — same
    shape as D3/ADR 0020's fix to `apply_payment`, one column over.
    """
    rows = db.query(
        "UPDATE balances SET past_due = past_due - %s, updated_at = now() "
        "WHERE loan_id = %s RETURNING past_due",
        (float(amount), loan_id),
    )
    new_past_due = rows[0]["past_due"] if rows else -float(amount)
    log.info(
        "waived fee loan_id=%s amount=%s new_past_due=%s", loan_id, amount, new_past_due
    )
    return new_past_due
