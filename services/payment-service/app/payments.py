"""Payment handling (moved verbatim from servicing-service's payments.py).

Stores the FULL PAN and the CVV on the payments row. Logs the full charge request
(PAN, CVV, SSN) at INFO. There is NO idempotency key — a retried POST inserts a second
payments row and applies the amount twice (double-charge). (D2, D5, #4, #7)

The amount is applied to the balance by calling servicing-service over HTTP (the
servicing /accounts/{loan_id}/apply-payment endpoint). ANY failure to apply -- servicing
unreachable (DNS/connection/timeout), a rejection (e.g. mismatched
INTERNAL_SERVICE_TOKEN), or a redirect -- reports "captured_unapplied" instead of a
plain "captured", because in every one of those cases the balance was NOT updated and
this codebase has no real reconciliation to fall back on (reconciliation.py's
reconciliation_peek is explicitly not run on a schedule and does not report breaks,
D7) -- see _apply_via_servicing.
"""

import json
import re
import uuid

import httpx

from .logging_config import get_logger
from . import db
from .config import INTERNAL_SERVICE_TOKEN, SERVICING_URL
from .redactor import PiiRedactor

log = get_logger("payment")  # writes to logs/payment-service.log

# The four fields every line on this path carries, in this order (spec D1(c)):
# request_id, loan_id, payment_id, outcome. A payment span is then recoverable by
# field extraction instead of by reading prose. These are named fields inside the
# ordinary text line, NOT JSON: the redactor scans the formatted message and two
# blocking gates (redaction-tests, redactor-drift) pin that byte sequence.
_SPAN_FIELDS = "request_id=%s loan_id=%s payment_id=%s outcome=%s"

# An id the log can hold on one line: no whitespace (a newline forges a whole
# record), no digits-only run long enough to look like a card to the redactor's
# backstop, capped so it cannot push the operational fields off the line.
_REQUEST_ID_OK = re.compile(r"[A-Za-z0-9._-]{1,64}")


def new_request_id(supplied: str = None) -> str:
    """The span's id: the caller's when it is one the log can carry, else a fresh one.

    D1(a) says a supplied X-Request-Id is used VERBATIM, and it is -- for any real
    id. The header is client-controlled free text on its way into a log line
    though, so a value outside the accepted charset is replaced rather than
    written through: a newline in it would forge a second log record, and a long
    digit run would ride past the value-level masking in _redacted_charge_req.
    Replacing (not blanking) keeps the span correlated under a usable id.
    """
    if supplied and _REQUEST_ID_OK.fullmatch(supplied):
        return supplied
    return uuid.uuid4().hex


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
    # The ENTRY line: it opens the span before any work is done, so it reports
    # outcome=started and carries no payment id yet -- the row does not exist
    # until the INSERT below. This line used to end "-> ok" while sitting in
    # exactly this position, asserting a success for an INSERT that had not run
    # and could still fail (spec D1(d), criterion 3). The real outcome is logged
    # once, at the end, when it is known.
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

    # Apply the captured amount to the balance via servicing-service. ANY
    # failure to apply -- rejected, redirected, or servicing unreachable --
    # means the balance was definitely not updated, and the response must say
    # so rather than report a normal "captured" success (Codex review, PR 32).
    applied = _apply_via_servicing(loan_id, amount, payment_id, request_id)
    status = "captured" if applied else "captured_unapplied"
    # The OUTCOME line: after the INSERT and after the apply attempt, carrying
    # what actually happened. Same id as the entry line and as servicing's own
    # line, so both halves of the span come back from one field query.
    log.info(
        "POST /payments charge complete " + _SPAN_FIELDS,
        request_id,
        loan_id,
        payment_id if payment_id is not None else "-",
        status,
    )
    return {
        "payment_id": payment_id,
        "loan_id": loan_id,
        "status": status,
        "applied_amount": float(amount) if applied else 0.0,
    }


def _apply_via_servicing(
    loan_id: int, amount: float, payment_id: int, request_id: str = "-"
) -> bool:
    """Tell servicing-service to apply this payment to the loan balance.

    Returns True only when servicing actually confirmed the apply (a 2xx
    response). Returns False for every other outcome -- servicing unreachable
    (connect/timeout/DNS, caught below), a rejection (e.g. mismatched
    INTERNAL_SERVICE_TOKEN), or a redirect (httpx does not follow redirects by
    default, so a 3xx means the apply-payment handler never ran).

    An earlier version of this fix treated "unreachable" as still-applied,
    reasoning the card was already charged and this would be "reconciled
    later" -- but nothing in this codebase actually reconciles later:
    reconciliation.py's reconciliation_peek is explicitly not run on a
    schedule and does not report breaks (D7). An unresolved network failure
    is exactly as permanently unresolved as a rejection, so charge() must
    treat them the same way (Codex review, PR 32, third round).
    """
    url = f"{SERVICING_URL}/accounts/{loan_id}/apply-payment"
    try:
        resp = httpx.post(
            url,
            # The id travels as a HEADER. The body is NOT changed -- it stays
            # {"amount", "payment_id"}, which week-5 D3(d) owns (it removes
            # `amount` from it), and a second writer on the same body collides
            # with that work. Spec D1(b).
            json={"amount": amount, "payment_id": payment_id},
            headers={
                "X-Internal-Service": INTERNAL_SERVICE_TOKEN,
                "X-Request-Id": request_id,
            },
            timeout=5.0,
        )
    except Exception as exc:
        log.error(
            "apply-payment call to servicing unreachable " + _SPAN_FIELDS + ": %s",
            request_id,
            loan_id,
            payment_id,
            "apply_unreachable",
            exc,
        )
        return False
    if not (200 <= resp.status_code < 300):
        log.error(
            "apply-payment REJECTED by servicing " + _SPAN_FIELDS + " status=%s",
            request_id,
            loan_id,
            payment_id,
            "apply_rejected",
            resp.status_code,
        )
        return False
    log.info(
        "applied payment via servicing " + _SPAN_FIELDS + " amount=%s",
        request_id,
        loan_id,
        payment_id,
        "applied",
        amount,
    )
    return True
