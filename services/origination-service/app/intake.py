"""Application intake + the LOS->LSS 'boarding' seam.

A funded loan is boarded to servicing by a DIRECT INSERT into the servicing tables
(`loans`, `balances`) from this origination code path. No boarding API, no event,
no contract. (brownfield seam #1 — see docs/architecture.md, ADR 0002)
"""

import secrets
from datetime import datetime, timedelta, timezone

from .logging_config import get_logger
from . import authz, config, db

log = get_logger("intake")


def ssn_last4(ssn: str | None) -> str | None:
    """Last 4 characters of an SSN -- enough for kyc-service's presence check
    (ssn_verified = bool(value)) without putting the full value on the wire (D33,
    docs/debt-log.md). A slice, not a digit strip: preserves bool()-truthiness of the
    original value exactly (any non-empty ssn yields a non-empty result), so routing
    KYC through this is not a behaviour change. None in, None out."""
    return ssn[-4:] if ssn else None


def create_application(
    payload: dict, submitted_by_user_id: int | None = None
) -> tuple[int, str]:
    """Insert applicant + application; return (app_id, RAW continuation_token).

    submitted_by_user_id is the caller's users.id when the submit was authenticated (the
    gateway forwards X-User-Id for any session-bearing request), else None for a genuinely
    anonymous apply. Persisted so deny_self_decision (D24, docs/debt-log.md) can refuse an
    officer who submitted their own application through this flow -- a case
    users.applicant_id == applications.applicant_id cannot see, because this INSERT never
    links the two.

    The RAW token is returned to the applicant exactly once (here); only its keyed hash is
    persisted, and it is stamped with an expiry (PR #7 review) so authz can time-box the
    bearer capability. See authz.hash_token / authz.require_officer_or_owner.

    Logs an ALLOWLIST of non-PII, non-free-text fields only (amount / term /
    entity flag) — never the raw payload. Dumping the whole request dict put
    client-controlled free text (name, address, ssn, ...) into the log, where a
    PAN could hide behind separators the whole-line redactor can't fully catch.
    Not logging free text at all removes that entire class (closes D5 on this
    path); the redactor stays a backstop for anything that still reaches a log.

    The ADR 0010 continuation token is generated here and persisted in the SAME
    application INSERT (PR review): a logged-out applicant's only credential must be
    durable with the row it authorizes. A separate best-effort UPDATE could leave a
    committed application with a NULL token and no recovery path, so if the INSERT fails
    the whole submit fails — never a persisted application without a usable token.
    """
    log.info(
        "POST /applications intake amount=%s term_months=%s is_entity=%s",
        payload.get("amount"),
        payload.get("term_months", 36),
        payload.get("is_entity", False),
    )
    # Hash the token BEFORE any write (PR #7 review). hash_token raises when the continuation
    # pepper is misconfigured; doing it first means that failure aborts submit with NOTHING
    # persisted, rather than after the applicant row is already committed. The applicant and the
    # application (with its token hash) are then written in ONE transaction, so a failure on the
    # second insert rolls back the first -- never an orphaned applicant PII row with no
    # application, no token, and no app id for the gateway compensator to target. db.query's
    # per-statement autocommit could leave exactly that orphan, so this path does not use it.
    continuation_token = secrets.token_urlsafe(32)  # 256-bit; returned raw once, below
    token_hash = authz.hash_token(
        continuation_token
    )  # keyed hash; raises here if misconfigured
    expires_at = datetime.now(timezone.utc) + timedelta(
        days=config.CONTINUATION_TOKEN_TTL_DAYS
    )
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO applicants (name, dob, ssn, ssn_last4, ein, is_entity, address) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                payload.get("name"),
                payload.get("dob"),
                payload.get("ssn"),
                ssn_last4(payload.get("ssn")),
                payload.get("ein"),
                payload.get("is_entity", False),
                payload.get("address"),
            ),
        )
        applicant_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO applications "
            "(applicant_id, amount, term_months, purpose, income, monthly_debt, "
            "employment_years, continuation_token, continuation_token_expires_at, "
            "submitted_by_user_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                applicant_id,
                payload.get("amount"),
                payload.get("term_months", 36),
                payload.get("purpose"),
                payload.get("income"),
                payload.get("monthly_debt"),
                payload.get("employment_years"),
                token_hash,  # store the keyed hash, never the raw
                expires_at,
                submitted_by_user_id,
            ),
        )
        app_id = cur.fetchone()["id"]
    # Return the RAW token to the caller (the only time it exists outside the applicant's
    # possession); the DB holds only its hash.
    return app_id, continuation_token


def board_to_servicing(
    app_id: int,
    applicant_name: str,
    principal: float,
    annual_rate_pct: float,
    term_months: int,
    note_rate_pct: float | None = None,
) -> int:
    """Direct cross-schema insert into the LSS tables. The 'seam'.

    `annual_rate_pct` is the disclosed actuarial APR, stored in `loans.apr` for display.
    `note_rate_pct` is the contractual rate servicing amortizes at — lower than the APR
    because the APR carries the prepaid origination fee. Boarding the APR as the servicing
    rate made the funded loan's schedule contradict its own TILA disclosure on every
    fee-bearing loan; accept_offer now derives the note rate from the delivered disclosure's
    compute_snapshot and passes it here. Defaults to `annual_rate_pct` for the legacy /board
    hatch, which supplies a single caller-supplied rate with no fee model to separate.
    """
    note_rate = note_rate_pct if note_rate_pct is not None else annual_rate_pct
    loan = db.query(
        "INSERT INTO loans (app_id, applicant_name, principal, apr, note_rate, term_months) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (app_id, applicant_name, principal, annual_rate_pct, note_rate, term_months),
    )
    loan_id = loan[0]["id"]
    # reach across into the servicing balances table directly
    db.query(
        "INSERT INTO balances (loan_id, balance) VALUES (%s, %s) "
        "ON CONFLICT (loan_id) DO NOTHING",
        (loan_id, float(principal)),  # money as float
    )
    log.info("boarded app_id=%s -> loan_id=%s (direct LSS insert)", app_id, loan_id)
    return loan_id


# build_disclosure was removed: offer/disclosure build moved to disclosure-service, which
# now persists the offers row itself. The offers router calls it over HTTP (see clients.py).
