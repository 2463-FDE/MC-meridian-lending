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

import hashlib
import json
import re
import uuid
from decimal import ROUND_HALF_UP, Decimal

import httpx

from .logging_config import get_logger
from . import db
from .config import (
    INTERNAL_SERVICE_TOKEN,
    SERVICING_URL,
    payment_idempotency_ttl_hours,
)
from .redactor import PiiRedactor

log = get_logger("payment")  # writes to logs/payment-service.log

# The four fields every line on this path carries, in this order (spec D1(c)):
# request_id, loan_id, payment_id, outcome. A payment span is then recoverable by
# field extraction instead of by reading prose. These are named fields inside the
# ordinary text line, NOT JSON: the redactor scans the formatted message and two
# blocking gates (redaction-tests, redactor-drift) pin that byte sequence.
_SPAN_FIELDS = "request_id=%s loan_id=%s payment_id=%s outcome=%s"

# An id the log can hold on one line: no whitespace (a newline forges a whole
# record), capped so it cannot push the operational fields off the line.
_REQUEST_ID_OK = re.compile(r"[A-Za-z0-9._-]{1,64}")

# How many digits a value may carry before it is refused as a possible SSN or
# card number. An SSN is 9 digits and a card is 13-19, so a 9-digit CEILING
# refuses both, and it refuses them by DIGIT COUNT rather than by shape: the
# separators and single letters this charset allows (412-55-9981, 4.1.2.5.5.9.9.8.1,
# 4111a1111a1111a1111) each defeat a positional pattern, and no arrangement of
# fewer than 9 digits can spell either value. The redactor does not cover this:
# it masks a bare SSN only inside a LABELED field (rule 3b) and a bare PAN only
# when Luhn-VALID (_redact_if_pan), so request_id=412559981 and an invalid-Luhn
# request_id=4111111111111112 both reach the log in cleartext. (Codex review)
_MAX_REQUEST_ID_DIGITS = 9
_ASCII_DIGIT = re.compile(r"[0-9]")

# servicing-service re-applies this SAME ceiling to whatever X-Request-Id it
# receives (services/servicing-service/app/main.py, _span_request_id) -- that
# route is reachable directly, so it re-validates rather than trusting the
# header. A raw uuid4 hex fails that check near-certainly (32 hex chars average
# ~20 digits), so a generated id was logged in full here but replaced with "-"
# on servicing's line -- the two halves of the span stopped sharing an id for
# every no-header or refused-header request (Codex review). Mapping each hex
# digit to a letter keeps uuid4's entropy and the accepted charset while
# guaranteeing zero digits, so the ceiling can never trigger on a generated id.
_HEX_DIGIT_TO_LETTER = str.maketrans("0123456789", "ghijklmnop")


def new_request_id(supplied: str = None) -> str:
    """The span's id: the caller's when it is one the log can carry, else a fresh one.

    D1(a) says a supplied X-Request-Id is used VERBATIM, and it is -- for any real
    id. The header is client-controlled free text on its way into a log line
    though, so a value outside the accepted charset, or one carrying enough
    digits to be an SSN or a card number, is replaced rather than written
    through: a newline in it would forge a second log record, and either PII
    shape would ride past the value-level masking in _redacted_charge_req.
    Replacing (not blanking) keeps the span correlated under a usable id.

    The digit ceiling costs a caller whose ids are digit-heavy (an epoch-millis
    id is 13 digits) its verbatim id; that call keeps a generated one and stays
    correlated end to end, which is the trade this path takes over logging a
    value that may be a borrower's SSN.

    The charset check runs FIRST, so the digit count only ever reads ASCII --
    a unicode digit cannot reach it.
    """
    if (
        supplied
        and _REQUEST_ID_OK.fullmatch(supplied)
        and len(_ASCII_DIGIT.findall(supplied)) < _MAX_REQUEST_ID_DIGITS
    ):
        return supplied
    # Not subject to the ceiling check above: this value carries no caller
    # input, so a random digit run in it is not a borrower's SSN. It still has
    # to survive servicing-service's own re-application of that ceiling on the
    # header it receives -- see _HEX_DIGIT_TO_LETTER above.
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
    """
    return {
        "pan": PiiRedactor._mask_pan_value(pan) if pan else pan,
        "cvv": "••••" if cvv else cvv,
        "ssn": _mask_ssn(ssn),
        "amount": amount,
        "loan_id": loan_id,
    }


# --- D19: idempotency ---------------------------------------------------------------
#
# A retried or double-clicked POST used to insert a second payments row and charge the
# card again (measured 2026-08-02: one $100 intent sent eight ways captured $800.00).
# The guarantee is the PARTIAL unique index on payments.idempotency_key, not this code:
# two handlers write this table (debt D23) and a support engineer with psql is a third,
# so the constraint is what makes "exactly one row per key" true for all of them. This
# function is the claim against that constraint, plus the branching D1 specifies for a
# caller who loses it.
#
# ADR 0013 Decision 1 and docs/spec-payments-week5.md D1-D2 own the semantics.

# Only a TERMINAL intent releases its key. An ACH row sits `submitted` for days and
# routinely outlives the window; retiring its key would free the value for a NEW charge
# while the original intent is still live -- reintroducing the exact double charge this
# closes. So an unfinished intent keeps its key past the window and answers 409.
_TERMINAL_STATUSES = ("captured", "failed", "settled", "returned")

# What charge() reports back to the route, which maps each to a status code.
CLAIMED = "claimed"  # we own this intent; proceed
REPLAY = "replay"  # same key, same payload, finished -> return the original
FINGERPRINT_MISMATCH = "fingerprint_mismatch"  # same key, DIFFERENT payload -> 422
IN_FLIGHT = "in_flight"  # same key, prior intent not finished -> 409

# The conflict target MUST spell the index predicate. The arbiter is a partial unique
# index, so a bare `ON CONFLICT (idempotency_key)` matches no arbiter and Postgres
# raises "there is no unique or exclusion constraint matching the ON CONFLICT
# specification" at runtime -- the insert failing before it can claim the key, which
# disables the whole control on first use. Vector R-DDL runs this literal string against
# a real Postgres for that reason.
#
# Insert-FIRST, never read-check-then-insert: the key is claimed before any capture
# work. Two concurrent identical requests both reach this statement and exactly one gets
# a row, which is the property no amount of application-level checking provides.
_CLAIM_SQL = (
    "INSERT INTO payments (loan_id, pan, cvv, amount, amount_minor, method, status, "
    "idempotency_key, idempotency_expires_at, request_fingerprint, "
    "processor_idempotency_key, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, 'processing', %s, now() + %s * INTERVAL '1 hour', "
    "%s, %s, now()) "
    "ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING "
    "RETURNING id"
)

# Retirement is a SINGLE statement whose WHERE stops matching the moment the key is
# NULL, so concurrent expired-key requests serialize on the row: under READ COMMITTED
# the second re-evaluates against the committed row, matches nothing, and updates zero
# rows. Exactly one caller can then win the re-claim; the losers get the ordinary 409.
# That is why retirement is not "read the timestamp and decide in the service".
_RETIRE_SQL = (
    "UPDATE payments SET idempotency_key = NULL, updated_at = now() "
    "WHERE idempotency_key = %s "
    "AND idempotency_expires_at <= now() "
    "AND status IN ('captured', 'failed', 'settled', 'returned') "
    "RETURNING id"
)

_READ_BY_KEY_SQL = (
    "SELECT id, loan_id, amount, amount_minor, method, status, request_fingerprint, "
    "idempotency_expires_at <= now() AS expired "
    "FROM payments WHERE idempotency_key = %s"
)

_FINALIZE_SQL = "UPDATE payments SET status = %s, updated_at = now() WHERE id = %s"


def _amount_minor(amount) -> int:
    """Integer minor units, via Decimal on the STRING form of the amount.

    float(amount) * 100 is not safe here: 25000.29 * 100 is 2500028.9999999995 and
    int() would truncate a cent off the fingerprint AND off the stored amount_minor.
    Decimal(str(...)) reads the decimal the caller actually sent. ROUND_HALF_UP is the
    money convention, not Python's default banker's rounding.
    """
    return int(
        Decimal(str(amount)).scaleb(2).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def request_fingerprint(loan_id, amount_minor, method, pan) -> str:
    """What distinguishes a genuine replay from a client that reused a key.

    Serialized with json.dumps over a LIST, so field boundaries are unambiguous: a
    delimiter-joined string would let ("a|b", "c") and ("a", "b|c") hash equal, and
    loan_id/method are caller-influenced.

    The instrument is a sha256 of the PAN, never the PAN itself. The specified
    fingerprint covers `card_token`/`bank_token`, and NEITHER COLUMN EXISTS -- they
    arrive with tokenization (ADR 0013 Decision 2, debt D13), which is a much larger
    change than this one. Two consequences, both deliberate and both recorded in the
    debt log rather than papered over:

    - This stores a card-derived value beside the PAN that ADR 0013 wants deleted. It is
      a one-way hash, not a second copy of the card, and it disappears when D13 replaces
      it with the token. Hashing rather than storing the PAN is what keeps R3b
      satisfiable at all today.
    - Vector R3c (two ACH submissions reusing one key against DIFFERENT bank accounts
      must be refused 422) is NOT satisfiable here, because this codebase has no bank
      instrument field at all -- an ACH request carries only loan_id, amount and method.
      Those two requests hash equal and the second is answered as a replay. R3c is
      unsatisfiable until D4b adds `bank_token`, and it is left red rather than deleted.
    """
    instrument = hashlib.sha256(pan.encode("utf-8")).hexdigest() if pan else None
    canonical = json.dumps(
        [loan_id, amount_minor, method, instrument], separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_by_key(key: str):
    rows = db.query(_READ_BY_KEY_SQL, (key,))
    return rows[0] if rows else None


def _claim(key, loan_id, pan, cvv, amount, amount_minor, method, fingerprint):
    rows = db.query(
        _CLAIM_SQL,
        (
            loan_id,
            pan,
            cvv,
            float(amount),
            amount_minor,
            method,
            key,
            payment_idempotency_ttl_hours(),
            fingerprint,
            # Deliberately NOT the client's Meridian key. If we forwarded that value,
            # the two retention windows would be coupled across vendors we do not
            # co-version: a legitimately new payment reusing a retired Meridian key
            # would reach the processor under the same value, and a processor whose own
            # retention outlives ours would collapse it as a replay -- a charge
            # suppressed there but credited here. Binding it per ROW breaks the
            # coupling, and its own partial unique index makes a drifted generator a
            # refused write rather than a split-brain charge.
            uuid.uuid4().hex,
        ),
    )
    return rows[0]["id"] if rows else None


def claim_or_branch(key, loan_id, pan, cvv, amount, amount_minor, method, fingerprint):
    """Claim the key, or report which of D1's answers this caller gets.

    Returns (outcome, payment_id, existing_row). At most TWO claim attempts are ever
    made, so no request can loop.
    """
    args = (key, loan_id, pan, cvv, amount, amount_minor, method, fingerprint)
    payment_id = _claim(*args)
    if payment_id is not None:
        return CLAIMED, payment_id, None

    existing = _read_by_key(key)
    if existing is None:
        # The holder retired its key between our INSERT and this SELECT, so the value is
        # free again. One bounded re-claim; if that also loses, another caller took it.
        payment_id = _claim(*args)
        if payment_id is not None:
            return CLAIMED, payment_id, None
        existing = _read_by_key(key)
        if existing is None:
            # Racing retirement, twice. Answer the retryable 409 rather than raise: the
            # caller's next attempt resolves it, and no charge has happened.
            return IN_FLIGHT, None, None

    # 2a. Expired AND terminal -> retire the key and take it. The UPDATE is the
    # serialization point; if it moved zero rows another caller retired it first and we
    # fall through to 2b against whatever row holds the key now.
    if existing["expired"] and existing["status"] in _TERMINAL_STATUSES:
        if db.query(_RETIRE_SQL, (key,)):
            payment_id = _claim(*args)
            if payment_id is not None:
                return CLAIMED, payment_id, None
        existing = _read_by_key(key) or existing

    # 2b. A live key, or an expired one on an intent still in flight.
    #
    # The fingerprint is checked FIRST, before the in-flight test. A reused key carrying
    # a different payload is a client defect whatever the prior intent's state, and
    # answering it 409 ("retry shortly") would invite the client to keep retrying a
    # request that can never be accepted under that key.
    if existing["request_fingerprint"] != fingerprint:
        return FINGERPRINT_MISMATCH, None, existing
    if existing["status"] not in _TERMINAL_STATUSES:
        return IN_FLIGHT, None, existing
    return REPLAY, existing["id"], existing


def charge(
    loan_id: int,
    pan: str,
    cvv: str,
    amount: float,
    ssn: str = None,
    name: str = None,
    method: str = "card",
    request_id: str = None,
    idempotency_key: str = None,
) -> dict:
    """Capture a payment, at most once per idempotency key.

    `idempotency_key` is required and client-minted (ADR 0013 Decision 1); the route
    refuses a request without a well-formed one before reaching here, so a None key
    means an internal caller and is a programming error, not a client error.
    """
    if not idempotency_key:
        raise ValueError("charge() requires an idempotency_key (ADR 0013 Decision 1)")
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
    # Claim the key BEFORE any capture work (D19). The partial unique index is the
    # enforcement point; this is the claim against it plus D1's branching for a caller
    # that loses the race. Every branch other than CLAIMED returns without capturing.
    amount_minor = _amount_minor(amount)
    fingerprint = request_fingerprint(loan_id, amount_minor, method, pan)
    outcome, payment_id, existing = claim_or_branch(
        idempotency_key,
        loan_id,
        pan,
        cvv,
        amount,
        amount_minor,
        method,
        fingerprint,
    )
    if outcome != CLAIMED:
        log.info(
            "POST /payments charge not captured " + _SPAN_FIELDS,
            request_id,
            loan_id,
            payment_id if payment_id is not None else "-",
            outcome,
        )
        return {
            "payment_id": payment_id,
            "loan_id": loan_id,
            # A replay reconstructs its response from the persisted row rather than
            # from a stored response snapshot, which would be a second source of truth
            # for the same facts.
            "status": existing["status"] if outcome == REPLAY else outcome,
            "applied_amount": float(existing["amount"] or 0.0)
            if outcome == REPLAY
            else 0.0,
            "request_id": request_id,
            "idempotency": outcome,
        }

    # Apply the captured amount to the balance via servicing-service. ANY
    # failure to apply -- rejected, redirected, or servicing unreachable --
    # means the balance was definitely not updated, and the response must say
    # so rather than report a normal "captured" success (Codex review, PR 32).
    applied = _apply_via_servicing(loan_id, amount, payment_id, request_id)
    status = "captured" if applied else "captured_unapplied"
    # Move the row off `processing` to a TERMINAL status either way. The card was
    # captured in both branches -- unapplied describes the balance, not the capture --
    # and a row left `processing` would hold its key forever, so every later retry of a
    # finished payment would answer 409 instead of replaying.
    #
    # Residual: the ROW cannot distinguish captured-and-applied from captured-unapplied,
    # so a replay of a request that first returned 424 returns a 200 replay instead.
    # Closing that needs the payment_applications record (spec D3(c): "unapplied is the
    # absence of a record"), which is D3d's change, not this one.
    db.query(_FINALIZE_SQL, ("captured", payment_id))
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
        "idempotency": CLAIMED,
        # The effective id: the caller's own when it was usable, else the
        # generated replacement -- returned so a caller whose id was refused
        # can still learn what the log lines are keyed on (Codex review).
        "request_id": request_id,
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
