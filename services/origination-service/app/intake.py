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


def create_application(payload: dict) -> tuple[int, str]:
    """Insert applicant + application; return (app_id, RAW continuation_token).

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
    applicant = db.query(
        "INSERT INTO applicants (name, dob, ssn, ein, is_entity, address) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (
            payload.get("name"),
            payload.get("dob"),
            payload.get("ssn"),
            payload.get("ein"),
            payload.get("is_entity", False),
            payload.get("address"),
        ),
    )
    applicant_id = applicant[0]["id"]
    continuation_token = secrets.token_urlsafe(32)  # 256-bit; returned raw once, below
    expires_at = datetime.now(timezone.utc) + timedelta(
        days=config.CONTINUATION_TOKEN_TTL_DAYS
    )
    app_row = db.query(
        "INSERT INTO applications "
        "(applicant_id, amount, term_months, purpose, income, monthly_debt, "
        "employment_years, continuation_token, continuation_token_expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            applicant_id,
            payload.get("amount"),
            payload.get("term_months", 36),
            payload.get("purpose"),
            payload.get("income"),
            payload.get("monthly_debt"),
            payload.get("employment_years"),
            authz.hash_token(continuation_token),  # store the keyed hash, never the raw
            expires_at,
        ),
    )
    # Return the RAW token to the caller (the only time it exists outside the applicant's
    # possession); the DB holds only its hash.
    return app_row[0]["id"], continuation_token


def board_to_servicing(
    app_id: int,
    applicant_name: str,
    principal: float,
    annual_rate_pct: float,
    term_months: int,
) -> int:
    """Direct cross-schema insert into the LSS tables. The 'seam'."""
    loan = db.query(
        "INSERT INTO loans (app_id, applicant_name, principal, apr, term_months) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (app_id, applicant_name, principal, annual_rate_pct, term_months),
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
