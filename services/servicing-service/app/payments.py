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
import uuid

from .logging_config import get_logger
from . import db, balance
from .redactor import PiiRedactor

log = get_logger("payment")  # writes to logs/payment-service.log

# Mirrors payment-service's payments.py exactly (D1(a)/(c), Codex review PR
# 41): this route is a SECOND front door for the same charge path (ADR 0004 --
# servicing kept the original pre-split implementation), not the downstream
# apply-payment half, so it mints its own id rather than reusing
# _span_request_id's verbatim-or-"-" rule below.
_SPAN_FIELDS = "request_id=%s loan_id=%s payment_id=%s outcome=%s"
_REQUEST_ID_OK = re.compile(r"[A-Za-z0-9._-]{1,64}")
_MAX_REQUEST_ID_DIGITS = 9
_ASCII_DIGIT = re.compile(r"[0-9]")
_HEX_DIGIT_TO_LETTER = str.maketrans("0123456789", "ghijklmnop")


def new_request_id(supplied: str = None) -> str:
    """The span's id: the caller's when it is one the log can carry, else a
    fresh one. Mirrors payment-service.payments.new_request_id -- see that
    docstring for the charset/digit-ceiling rationale."""
    if (
        supplied
        and _REQUEST_ID_OK.fullmatch(supplied)
        and len(_ASCII_DIGIT.findall(supplied)) < _MAX_REQUEST_ID_DIGITS
    ):
        return supplied
    return uuid.uuid4().hex.translate(_HEX_DIGIT_TO_LETTER)


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


def charge(
    loan_id: int,
    pan: str,
    cvv: str,
    amount: float,
    ssn: str = None,
    name: str = None,
    method: str = "card",
    request_id: str = None,
) -> dict:
    request_id = new_request_id(request_id)
    # The ENTRY line: opens the span before the INSERT runs, so it reports
    # outcome=started and carries no payment id yet. The prior line asserted
    # "-> ok" from this same position -- a success for an INSERT that had not
    # run and could still fail -- the same defect D1(d) fixed on
    # payment-service's side of this identical route shape.
    #
    # PII (PAN/CVV/SSN) is masked at the value level before it reaches the log
    # string; see _redacted_charge_req for why the old formatter-only approach
    # was bypassable. `name` is client-controlled free text (a PAN can be
    # smuggled into it) and is deliberately not logged. json.dumps escapes any
    # quotes in the remaining values.
    log.info(
        "POST /payments charge " + _SPAN_FIELDS + " req=%s",
        request_id,
        loan_id,
        "-",
        "started",
        json.dumps(
            _redacted_charge_req(pan, cvv, ssn, amount, loan_id), ensure_ascii=False
        ),
    )
    # No idempotency check. No unique charge reference. Every POST inserts a row.
    rows = db.query(
        "INSERT INTO payments (loan_id, pan, cvv, amount, method) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (loan_id, pan, cvv, float(amount), method),  # full PAN + CVV persisted
    )
    payment_id = rows[0]["id"] if rows else None
    new_balance = balance.apply_payment(loan_id, amount)
    # The OUTCOME line: after the INSERT and the balance mutation, carrying
    # what actually happened, keyed by the same id as the entry line.
    log.info(
        "POST /payments charge complete " + _SPAN_FIELDS,
        request_id,
        loan_id,
        payment_id if payment_id is not None else "-",
        "captured",
    )
    return {
        "loan_id": loan_id,
        "amount": amount,
        "balance": new_balance,
        "payment_id": payment_id,
        "request_id": request_id,
    }
