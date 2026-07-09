"""Payment handling (formerly the vendor's prototype 'pay.py').

Stores the FULL PAN and the CVV on the payments row (PCI storage debt, D5 — not
addressed here). The charge LOG is now redacted at the construction boundary:
PAN/CVV/SSN are masked at the value level before interpolation and `name` free
text is not logged (mirrors payment-service). There is NO idempotency key — a
retried POST inserts a second payments row and applies the amount twice
(double-charge). (D2, D5, #4, #7)
"""
import json
import re

from .logging_config import get_logger
from . import db, balance
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

    Kept byte-identical to payment-service.charge's redaction boundary — both
    services expose the same POST /payments charge path and must not diverge.
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
    db.query(
        "INSERT INTO payments (loan_id, pan, cvv, amount, method) "
        "VALUES (%s, %s, %s, %s, %s)",
        (loan_id, pan, cvv, float(amount), method),   # full PAN + CVV persisted
    )
    new_balance = balance.apply_payment(loan_id, amount)
    return {"loan_id": loan_id, "amount": amount, "balance": new_balance}
