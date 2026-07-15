"""Application intake, listing, detail, decisioning, and acceptance/boarding."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clients, db, intake, models
from ..database import get_session
from ..logging_config import get_logger
from ..schemas import (
    ApplicationCreated,
    ApplicationDetail,
    ApplicationIn,
    ApplicationListItem,
    ApplicantOut,
    DecisionOut,
    Disclosure,
    KycOut,
    Page,
)

log = get_logger("applications")
router = APIRouter(prefix="/applications", tags=["applications"])


@router.post("", response_model=ApplicationCreated)
def submit_application(body: ApplicationIn):
    payload = body.model_dump()
    app_id = intake.create_application(
        payload
    )  # creates applicant+application rows; logs operational fields only (no PII)
    # Resolve applicant_id the same way the old in-process path did.
    applicant_id = None
    try:
        applicant_rows = db.query(
            "SELECT applicant_id FROM applications WHERE id = %s", (app_id,)
        )
        applicant_id = applicant_rows[0]["applicant_id"] if applicant_rows else None
    except Exception as e:  # noqa
        log.warning("could not resolve applicant_id: %s", e)

    # CIP/KYC moved to kyc-service. It persists its own kyc_checks row (so no INSERT here).
    # Default to all-false; a kyc-service hiccup must not 500 the intake (resilience kept).
    cip = {
        "name_verified": False,
        "dob_verified": False,
        "address_verified": False,
        "ssn_verified": False,
    }
    is_entity = bool(payload.get("is_entity"))
    try:
        resp = clients.post(
            clients.KYC_URL,
            "/kyc/check",
            {
                "application_id": app_id,
                "applicant_id": applicant_id,
                "name": payload.get("name"),
                "dob": payload.get("dob"),
                "ssn": payload.get("ssn"),
                "address": payload.get("address"),
                "entity_type": "llc" if is_entity else None,
            },
        )
        passed = bool(resp.get("cip_passed"))
        # Map kyc-service cip_passed -> the four KycOut booleans the frontend expects.
        # CIP verifies name/dob/address/ssn that were provided; entity applicants have no
        # dob/ssn so those stay false even on a pass (mirrors the old in-process stub).
        cip = {
            "name_verified": passed,
            "dob_verified": passed and not is_entity,
            "address_verified": passed,
            "ssn_verified": passed and not is_entity,
        }
    except Exception as e:  # noqa
        log.warning("kyc-service call failed: %s", e)
    return {"app_id": app_id, "status": "submitted", "kyc": KycOut(**cip)}


@router.get("", response_model=Page[ApplicationListItem])
def list_applications(
    session: Session = Depends(get_session),
    status: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    stmt = select(models.Application, models.Applicant.name).join(
        models.Applicant,
        models.Application.applicant_id == models.Applicant.id,
        isouter=True,
    )
    count_stmt = select(func.count(models.Application.id))
    if status:
        stmt = stmt.where(models.Application.status == status)
        count_stmt = count_stmt.where(models.Application.status == status)
    total = session.scalar(count_stmt) or 0
    stmt = stmt.order_by(models.Application.id.desc()).limit(limit).offset(offset)
    items = [
        ApplicationListItem(
            id=a.id,
            applicant_name=name,
            amount=a.amount,
            term_months=a.term_months,
            purpose=a.purpose,
            status=a.status,
            created_at=a.created_at.isoformat() if a.created_at else None,
        )
        for a, name in session.execute(stmt).all()
    ]
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/{app_id}", response_model=ApplicationDetail)
def get_application(app_id: int, session: Session = Depends(get_session)):
    a = session.get(models.Application, app_id)
    if not a:
        raise HTTPException(status_code=404, detail="application not found")
    applicant = a.applicant
    kyc_row = (
        session.scalar(
            select(models.KycCheck)
            .where(models.KycCheck.applicant_id == a.applicant_id)
            .order_by(models.KycCheck.id.desc())
        )
        if a.applicant_id
        else None
    )
    dec = session.get(models.Decision, app_id)
    offer = session.scalar(
        select(models.Offer)
        .where(models.Offer.app_id == app_id)
        .order_by(models.Offer.id.desc())
    )
    return ApplicationDetail(
        id=a.id,
        applicant=ApplicantOut(
            id=applicant.id,
            name=applicant.name,
            email=applicant.email,
            phone=applicant.phone,
            address=applicant.address,
            is_entity=applicant.is_entity,
        )
        if applicant
        else None,
        amount=a.amount,
        term_months=a.term_months,
        purpose=a.purpose,
        status=a.status,
        employer=a.employer,
        job_title=a.job_title,
        kyc=KycOut(
            name_verified=bool(kyc_row.name_verified),
            dob_verified=bool(kyc_row.dob_verified),
            address_verified=bool(kyc_row.address_verified),
            ssn_verified=bool(kyc_row.ssn_verified),
        )
        if kyc_row
        else None,
        decision=dec.outcome if dec else None,
        offer=Disclosure(
            apr=offer.apr or 0,
            finance_charge=offer.finance_charge or 0,
            monthly_payment=offer.monthly_payment or 0,
            amount_financed=offer.amount_financed or 0,
            total_of_payments=offer.total_of_payments or 0,
        )
        if offer
        else None,
    )


def decision_request_payload(app_id: int) -> dict:
    """Build the decision-service request for an application from the LOS database.

    Also the assistant's score tool (app/assistant.py): applicant data is looked up
    here by code — the model supplies only an application id, never applicant fields.
    Returns None when the application does not exist.
    """
    rows = db.query(
        "SELECT a.id, a.applicant_id, a.amount, a.term_months, a.income, "
        "a.employment_years, ap.name, ap.ssn "
        "FROM applications a LEFT JOIN applicants ap ON ap.id = a.applicant_id WHERE a.id = %s",
        (app_id,),
    )
    if not rows:
        return None
    r = rows[0]
    return {
        "application_id": app_id,
        "applicant_id": r.get("applicant_id"),
        "name": r.get("name"),
        "ssn": r.get("ssn") or "",
        "requested_amount": r.get("amount"),
        "term_months": r.get("term_months"),
        "annual_income": r.get("income") or 0,
        "monthly_debt": 0,  # not captured in the LOS today
        "employment_years": r.get("employment_years") or 0,
        "credit_score": None,  # pulled downstream by decision-service
    }


@router.post("/{app_id}/decision", response_model=DecisionOut)
def run_decision(app_id: int):
    payload = decision_request_payload(app_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="application not found")
    # Decisioning moved to decision-service; it persists the decision_events record.
    resp = clients.post(clients.DECISION_URL, "/decisions", payload)
    return DecisionOut(
        app_id=app_id,
        decision=resp["outcome"],
        score=int(round(resp.get("score") or 0)),  # DecisionOut.score is int
        adverse_action_reason=resp.get("reason"),
    )


@router.post("/{app_id}/accept")
def accept_offer(app_id: int):
    rows = db.query(
        "SELECT a.amount, a.term_months, ap.name, o.apr "
        "FROM applications a LEFT JOIN applicants ap ON ap.id = a.applicant_id "
        "LEFT JOIN offers o ON o.app_id = a.id WHERE a.id = %s ORDER BY o.id DESC",
        (app_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="application not found")
    r = rows[0]
    rate = r.get("apr") or 7.99
    loan_id = intake.board_to_servicing(
        app_id, r.get("name") or "Borrower", r["amount"], rate, r["term_months"]
    )
    db.query("UPDATE applications SET status = 'funded' WHERE id = %s", (app_id,))
    return {"loan_id": loan_id}
