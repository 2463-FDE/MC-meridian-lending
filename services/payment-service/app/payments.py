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


def _scrub_free_text_pan(text: str) -> str:
    """Mask a PAN hidden anywhere in a short free-text value (e.g. `name`),
    separator-agnostic and BOUND-FREE.

    The shared PiiRedactor free-text pass bounds the separator run (<=3 chars
    between digits) so it stays safe when applied to a whole log line. But a
    client can defeat any fixed bound (4111====1111====...), so for the isolated,
    short name value we detect digit runs separated by ANY number of non-
    alphanumeric chars and Luhn-check the extracted digits — exactly the
    digit-extraction + Luhn approach, with no separator or length enumeration.
    The Luhn gate keeps ordinary numeric text (IDs, amounts) untouched. Guarded
    on a >=13 digit count so normal names skip the scan entirely.
    """
    if not text or sum(c.isdigit() for c in text) < 13:
        return text

    def _mask(m):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and PiiRedactor._luhn_valid(digits):
            return PiiRedactor._mask_with_last_4(digits) + " (PAN)"
        return m.group(0)

    return re.sub(r"\d(?:[^0-9A-Za-z]*\d){12,18}", _mask, str(text))


def _mask_ssn(ssn):
    """Mask an SSN value to •••-••-LAST4 (digit-count based, separator-agnostic)."""
    if not ssn:
        return ssn
    digits = re.sub(r"\D", "", str(ssn))
    if len(digits) >= 4:
        return "•••-••-" + digits[-4:]
    return "•" * len(str(ssn))


def _redacted_charge_req(pan, cvv, ssn, amount, loan_id, name) -> dict:
    """Charge-request fields with card/SSN values masked at the VALUE level,
    BEFORE anything is interpolated into the log string.

    This is the fix for the charge-log PAN bypass. The prior code built a
    hand-formatted pseudo-JSON string from the raw, client-controlled `pan` and
    relied on the log formatter's regex to redact it. That is defeatable by
    delimiter injection: a pan like `4111","x":"111111111111` splits the number
    across fake JSON fields, so the delimiter-sensitive formatter masks only a
    <13-digit fragment and the rest leaks in the clear. Masking each value here
    (PAN by digit count, so separators/injected quotes are stripped and cannot
    un-mask it) and serializing with `json.dumps` (which escapes embedded
    quotes) removes the pseudo-JSON parsing surface entirely. The formatter
    redaction stays on as a backstop.

    `name` is client-controlled free text, so a PAN can be smuggled into it with
    any separator (comma, tilde, backslash, ...) and any separator length. We do
    NOT enumerate separators or a length bound: _scrub_free_text_pan does bound-
    free digit-extraction + Luhn on the isolated value, then PiiRedactor.redact
    masks any other PII (email/ssn/phone) in the name.
    """
    return {
        "pan": PiiRedactor._mask_pan_value(pan) if pan else pan,
        "cvv": "••••" if cvv else cvv,
        "ssn": _mask_ssn(ssn),
        "amount": amount,
        "loan_id": loan_id,
        "name": PiiRedactor.redact(_scrub_free_text_pan(name)) if name else name,
    }


def charge(loan_id: int, pan: str, cvv: str, amount: float, ssn: str = None,
           name: str = None, method: str = "card") -> dict:
    # PII (PAN/CVV/SSN) is masked at the value level before it reaches the log
    # string; see _redacted_charge_req for why the old formatter-only approach
    # was bypassable. json.dumps escapes any quotes in free-text fields (name).
    log.info(
        "POST /payments charge req=%s -> ok",
        json.dumps(_redacted_charge_req(pan, cvv, ssn, amount, loan_id, name),
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
