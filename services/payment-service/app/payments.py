"""Payment handling (moved verbatim from servicing-service's payments.py).

Stores the FULL PAN and the CVV on the payments row. Logs the full charge request
(PAN, CVV, SSN) at INFO. There is NO idempotency key — a retried POST inserts a second
payments row and applies the amount twice (double-charge). (D2, D5, #4, #7)

The amount is applied to the balance by calling servicing-service over HTTP (the
servicing /accounts/{loan_id}/apply-payment endpoint). If servicing is unreachable the
charge is still reported captured so this service stands alone.
"""
import httpx

from .logging_config import get_logger
from . import db
from .config import SERVICING_URL

log = get_logger("payment")   # writes to logs/payment-service.log


def charge(loan_id: int, pan: str, cvv: str, amount: float, ssn: str = None,
           name: str = None, method: str = "card") -> dict:
    # Full request body — including PAN, CVV, SSN — at INFO. No redaction.
    log.info(
        'POST /payments charge req={"pan":"%s","cvv":"%s","ssn":"%s","amount":%s,'
        '"loan_id":%s,"name":"%s"} -> ok',
        pan, cvv, ssn, amount, loan_id, name,
    )
    # No idempotency check. No unique charge reference. Every POST inserts a row.
    rows = db.query(
        "INSERT INTO payments (loan_id, pan, cvv, amount, method) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (loan_id, pan, cvv, float(amount), method),   # full PAN + CVV persisted
    )
    payment_id = rows[0]["id"] if rows else None

    # Apply the captured amount to the balance via servicing-service.
    _apply_via_servicing(loan_id, amount, payment_id)
    return {
        "payment_id": payment_id,
        "loan_id": loan_id,
        "status": "captured",
        "applied_amount": float(amount),
    }


def _apply_via_servicing(loan_id: int, amount: float, payment_id: int) -> None:
    """Tell servicing-service to apply this payment to the loan balance."""
    url = f"{SERVICING_URL}/accounts/{loan_id}/apply-payment"
    try:
        resp = httpx.post(
            url, json={"amount": amount, "payment_id": payment_id}, timeout=5.0
        )
        resp.raise_for_status()
        log.info(
            "applied payment via servicing loan_id=%s payment_id=%s amount=%s -> ok",
            loan_id, payment_id, amount,
        )
    except Exception as exc:
        # Servicing unreachable / errored — the card was already charged and the row
        # written, so we still report the charge captured. (apply reconciled later)
        log.error(
            "apply-payment call to servicing failed loan_id=%s payment_id=%s: %s",
            loan_id, payment_id, exc,
        )
