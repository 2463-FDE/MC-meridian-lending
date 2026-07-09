"""Payment handling (moved verbatim from servicing-service's payments.py).

Stores the FULL PAN and the CVV on the payments row. Logs the full charge request
(PAN, CVV, SSN) at INFO. There is NO idempotency key — a retried POST inserts a second
payments row and applies the amount twice (double-charge). (D2, D5, #4, #7)

The amount is applied to the balance by calling servicing-service over HTTP (the
servicing /accounts/{loan_id}/apply-payment endpoint). If servicing is unreachable the
charge is still reported captured so this service stands alone.
"""
import json
import re

import httpx

from .logging_config import get_logger
from . import db
from .config import SERVICING_URL
from .redactor import PiiRedactor

log = get_logger("payment")   # writes to logs/payment-service.log


def _mask_ssn(ssn):
    """Mask an SSN value to •••-••-LAST4 (digit-count based, separator-agnostic)."""
    if not ssn:
        return ssn
    digits = re.sub(r"\D", "", str(ssn))
    if len(digits) >= 4:
        return "•••-••-" + digits[-4:]
    return "•" * len(str(ssn))


def _redacted_charge_req(pan, cvv, ssn, amount, loan_id) -> dict:
    """Charge-request fields for the log — an ALLOWLIST of operational values,
    with card/SSN masked at the VALUE level BEFORE anything is interpolated.

    Two prior bypasses are closed here. (1) Delimiter injection: the old code
    built a hand-formatted pseudo-JSON string from the raw, client-controlled
    `pan` and relied on the log formatter's regex; a pan like
    `4111","x":"111111111111` split the number across fake JSON fields so the
    formatter masked only a <13-digit fragment. Masking each value here (PAN by
    digit count, so injected separators/quotes are stripped) and serializing with
    `json.dumps` (which escapes embedded quotes) removes that parsing surface.
    (2) Free-text smuggling: `name` was logged after a Luhn-on-run scrub, but a
    leading ordinary digit (`Apt 12 4111x1111x...`) corrupts the extracted run so
    Luhn fails and the card passed through. Chasing that with a sliding window
    false-masks ordinary 13-19 digit IDs, so instead `name` (client-controlled
    free text, not needed operationally and not persisted) is simply NOT logged.
    No free text in the charge log = no place left to smuggle a PAN. The
    formatter redaction stays on as a backstop.
    """
    return {
        "pan": PiiRedactor._mask_pan_value(pan) if pan else pan,
        "cvv": "••••" if cvv else cvv,
        "ssn": _mask_ssn(ssn),
        "amount": amount,
        "loan_id": loan_id,
    }


def charge(loan_id: int, pan: str, cvv: str, amount: float, ssn: str = None,
           name: str = None, method: str = "card") -> dict:
    # PII (PAN/CVV/SSN) is masked at the value level before it reaches the log
    # string; see _redacted_charge_req for why the old formatter-only approach
    # was bypassable. `name` is client-controlled free text (a PAN can be
    # smuggled into it) and is deliberately not logged. json.dumps escapes any
    # quotes in the remaining values.
    log.info(
        "POST /payments charge req=%s -> ok",
        json.dumps(_redacted_charge_req(pan, cvv, ssn, amount, loan_id),
                   ensure_ascii=False),
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
