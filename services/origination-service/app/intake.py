"""Application intake + the LOS->LSS 'boarding' seam.

A funded loan is boarded to servicing by a DIRECT INSERT into the servicing tables
(`loans`, `balances`) from this origination code path. No boarding API, no event,
no contract. (brownfield seam #1 — see docs/architecture.md, ADR 0002)
"""
from .logging_config import get_logger
from . import db

log = get_logger("intake")


def create_application(payload: dict) -> int:
    """Insert applicant + application. Logs the full body including PII (D5)."""
    log.info("POST /applications intake req=%s", payload)  # full PII in the log
    applicant = db.query(
        "INSERT INTO applicants (name, dob, ssn, ein, is_entity, address) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (
            payload.get("name"), payload.get("dob"), payload.get("ssn"),
            payload.get("ein"), payload.get("is_entity", False), payload.get("address"),
        ),
    )
    applicant_id = applicant[0]["id"]
    app_row = db.query(
        "INSERT INTO applications (applicant_id, amount, term_months, purpose, income) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (
            applicant_id, payload.get("amount"), payload.get("term_months", 36),
            payload.get("purpose"), payload.get("income"),
        ),
    )
    return app_row[0]["id"]


def board_to_servicing(app_id: int, applicant_name: str, principal: float,
                       annual_rate_pct: float, term_months: int) -> int:
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
        (loan_id, float(principal)),   # money as float
    )
    log.info("boarded app_id=%s -> loan_id=%s (direct LSS insert)", app_id, loan_id)
    return loan_id


# build_disclosure was removed: offer/disclosure build moved to disclosure-service, which
# now persists the offers row itself. The offers router calls it over HTTP (see clients.py).
