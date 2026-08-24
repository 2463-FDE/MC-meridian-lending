"""Payment handling (formerly the vendor's prototype 'pay.py').

Stores the FULL PAN and the CVV on the payments row (PCI storage debt, D5 — not
addressed here). The charge LOG is now redacted at the construction boundary:
PAN/CVV/SSN are masked at the value level before interpolation and `name` free
text is not logged (mirrors payment-service). Idempotency is enforced by a
partial unique index on payments.idempotency_key with an insert-first claim
(claim_or_branch, D19, ADR 0013 Decision 1) — a retried POST under the same key
returns the original outcome instead of a second row. (D2, D5, #4, #7)
"""

import hashlib
import json
import re
import uuid
from decimal import ROUND_HALF_UP, Decimal

from .logging_config import get_logger
from . import db, balance
from .config import payment_idempotency_ttl_hours
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


# NOTE: this block is kept BYTE-IDENTICAL to payment-service/app/payments.py. ADR 0004
# copied the charge handler out of this service and left both routed (debt D23), and the
# client confirmed 2026-08-17 that the second path stays. Two writers of one table must
# answer a reused key the same way, so this is a deliberate copy in the shape of the
# per-service redactor copies -- diverge it and one route replays where the other 409s.
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
_TERMINAL_STATUSES = ("captured", "captured_unapplied", "failed", "settled", "returned")

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
#
# DELIBERATELY narrower than _TERMINAL_STATUSES: captured_unapplied is terminal for
# REPLAY purposes (a finished intent, so claim_or_branch answers it instead of 409ing
# forever) but NOT listed here for RETIREMENT. The card was actually charged and the
# balance was not; retiring the key would let a client's later retry mint a SECOND
# real charge under the same logical intent while the first sits unresolved -- exactly
# the double charge D19 exists to prevent. An expired captured_unapplied row keeps
# its key and keeps replaying 424/0 past the TTL (see
# test_expired_captured_unapplied_never_retires_or_double_charges) until the D3d
# resolution path (payment_applications) or an operator resolves it; it does not
# silently age into a fresh charge attempt like a genuine "failed" or "returned".
#
# The `captured` arm additionally requires a payment_applications row (D3 / ADR 0020).
# charge() now finalizes the row to `captured` BEFORE calling servicing, so a process that
# dies in that window leaves a `captured` row whose balance never moved -- retiring its key
# would let a later retry mint a second real charge, which is the hole D19 closed via
# captured_unapplied and which the reordering would otherwise reopen. Applied-ness is a
# fact of the record, not of the status string, so the record is what this asks.
_RETIRE_SQL = (
    "UPDATE payments SET idempotency_key = NULL, updated_at = now() "
    "WHERE idempotency_key = %s "
    "AND idempotency_expires_at <= now() "
    "AND status IN ('captured', 'failed', 'settled', 'returned') "
    "AND (status <> 'captured' OR EXISTS ("
    "    SELECT 1 FROM payment_applications pa WHERE pa.payment_id = payments.id)) "
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
    refuses a request without a well-formed one before reaching here.
    """
    if not idempotency_key:
        raise ValueError("charge() requires an idempotency_key (ADR 0013 Decision 1)")
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
    # Claim the key BEFORE any capture work (D19), against the same partial unique
    # index payment-service claims against -- the constraint, not this code, is what
    # makes "one row per key" true for both writers.
    amount_minor = _amount_minor(amount)
    fingerprint = request_fingerprint(loan_id, amount_minor, method, pan)
    outcome, payment_id, existing = claim_or_branch(
        idempotency_key, loan_id, pan, cvv, amount, amount_minor, method, fingerprint
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
            "loan_id": loan_id,
            # `amount` is what the CARD captured, not what the balance absorbed. This
            # route is a second writer of the SAME payments table (D23) -- a row this
            # replay reads back may have been captured_unapplied by payment-service's
            # handler, so only a status of "captured" means the balance genuinely got
            # this amount.
            "amount": float(existing["amount"] or 0.0)
            if outcome == REPLAY and existing["status"] == "captured"
            else 0.0,
            # No balance read on a non-capture: this request moved no money, and
            # reporting a balance it did not produce would invite the caller to treat
            # a refusal as a settled figure.
            "balance": None,
            "payment_id": payment_id,
            "request_id": request_id,
            "idempotency": outcome,
        }
    # Off `processing` to a terminal status: a row left processing would hold its key
    # forever, so every later retry of a finished payment would 409 instead of replay.
    #
    # Finalized BEFORE the apply, which inverts the D19 order (D3 / ADR 0020).
    # balance.apply_payment credits only a payments row whose status already says the card
    # was captured, so applying first and finalizing after would make the predicate refuse
    # every live payment. The window this opens — captured written, process dies, no
    # application row — is exactly what `payment_applications` now makes visible, and
    # _RETIRE_SQL refuses to retire a captured row that has no application row, so the
    # window cannot age into a second real charge.
    db.query(_FINALIZE_SQL, ("captured", payment_id))
    try:
        new_balance, _moved = balance.apply_payment(
            loan_id, payment_id, request_id=request_id
        )
        status = "captured"
    except balance.PaymentNotApplicable as exc:
        # The card was captured and the balance did not move. Say so in the row and in the
        # response rather than reporting a normal success — the same contract
        # payment-service's handler already answers 424 on (D19 B1).
        db.query(_FINALIZE_SQL, ("captured_unapplied", payment_id))
        status = "captured_unapplied"
        new_balance = None
        log.error(
            "POST /payments captured but not applied " + _SPAN_FIELDS + " reason=%s",
            request_id,
            loan_id,
            payment_id if payment_id is not None else "-",
            status,
            exc.reason,
        )
    # The OUTCOME line: after the INSERT and the balance mutation, carrying
    # what actually happened, keyed by the same id as the entry line.
    log.info(
        "POST /payments charge complete " + _SPAN_FIELDS,
        request_id,
        loan_id,
        payment_id if payment_id is not None else "-",
        status,
    )
    return {
        "loan_id": loan_id,
        # `amount` is what the balance absorbed, not what the card captured — the same
        # rule the replay branch above applies. An unapplied capture reports 0.0.
        "amount": amount if status == "captured" else 0.0,
        "balance": new_balance,
        "payment_id": payment_id,
        "request_id": request_id,
        "idempotency": CLAIMED,
        "status": status,
    }
