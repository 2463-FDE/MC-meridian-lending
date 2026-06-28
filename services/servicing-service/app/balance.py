"""Balance + payment application.

Money is float. The balance is a single column overwritten in place (no ledger,
no transaction history). The read-modify-write has no locking, so a payment and a
concurrent fee waiver on the same loan can lose an update. (D1, D3, D12, D14)
"""
from .logging_config import get_logger
from . import db

log = get_logger("balance")


def get_balance(loan_id: int) -> float:
    rows = db.query("SELECT balance FROM balances WHERE loan_id = %s", (loan_id,))
    return rows[0]["balance"] if rows else 0.0


def get_past_due(loan_id: int) -> float:
    rows = db.query("SELECT past_due FROM balances WHERE loan_id = %s", (loan_id,))
    return rows[0]["past_due"] if rows else 0.0


def apply_payment(loan_id: int, amount: float) -> float:
    """Read-modify-write with no lock. Float math. No waterfall (fees/interest/principal)."""
    current = get_balance(loan_id)             # READ
    new_balance = current - float(amount)      # MODIFY (float, straight off principal)
    db.query(                                  # WRITE (overwrite in place)
        "UPDATE balances SET balance = %s, updated_at = now() WHERE loan_id = %s",
        (new_balance, loan_id),
    )
    log.info("applied payment loan_id=%s balance %s -> %s", loan_id, current, new_balance)
    return new_balance


def adjust_balance(loan_id: int, new_value: float) -> float:
    """Set the balance directly. No ledger entry; the prior value is gone forever."""
    current = get_balance(loan_id)
    db.query(
        "UPDATE balances SET balance = %s, updated_at = now() WHERE loan_id = %s",
        (float(new_value), loan_id),
    )
    log.info("adjusted balance loan_id=%s %s -> %s", loan_id, current, new_value)
    return float(new_value)


def waive_fee(loan_id: int, amount: float) -> float:
    """Reduce past_due. Read-modify-write, no lock — races with apply_payment."""
    rows = db.query("SELECT past_due FROM balances WHERE loan_id = %s", (loan_id,))
    past_due = rows[0]["past_due"] if rows else 0.0
    new_past_due = past_due - float(amount)    # float
    db.query(
        "UPDATE balances SET past_due = %s, updated_at = now() WHERE loan_id = %s",
        (new_past_due, loan_id),
    )
    log.info("waived fee loan_id=%s past_due %s -> %s", loan_id, past_due, new_past_due)
    return new_past_due
