"""Application intake, listing, detail, decisioning, and acceptance/boarding."""

import hmac

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clients, config, db, intake, models
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
    MonthlyDebtIn,
    Page,
)

log = get_logger("applications")
router = APIRouter(prefix="/applications", tags=["applications"])


def _require_internal_caller(x_internal_service: str | None) -> None:
    """Gate a route to internal service-to-service callers (PR review).

    Mirrors decision-service's guard: the gateway strips any client-supplied
    X-Internal-Service header, so only a caller reaching this service directly with
    the shared secret is accepted. Fails closed when the token is unconfigured (503,
    never open); constant-time compare so the token can't be timed out byte-by-byte.
    """
    expected = config.INTERNAL_SERVICE_TOKEN
    if not expected:
        log.error("INTERNAL_SERVICE_TOKEN not configured; refusing internal route")
        raise HTTPException(status_code=503, detail="internal auth not configured")
    # Compare as bytes (see decision-service guard): avoids a TypeError-to-500 on a
    # non-ASCII token while staying constant-time.
    if not x_internal_service or not hmac.compare_digest(
        x_internal_service.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=403, detail="internal service identity required"
        )


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
    kyc_checked = True
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
        # A transport/auth failure (outage, timeout, or a missing/rotated internal token
        # -> 403) is NOT a KYC "not verified" result — the check never ran. A genuine
        # decline comes back 200 with cip_passed False, so only an exception reaches
        # here. Keep the deliberate intake resilience (a KYC hiccup must not 500 the
        # applicant), but do NOT let the failure masquerade as an ordinary all-false
        # verification (PR review): raise the log to error, record an audit_logs row so
        # an application created while KYC was down is queryable, and flag
        # kyc_checked=False so a caller can tell it apart from a real decline. Whether
        # such an application may proceed to decisioning is a separate policy decision
        # (KYC-gating ADR follow-up); this only stops the failure from being silent.
        kyc_checked = False
        log.error("kyc-service call failed for app_id=%s: %s", app_id, type(e).__name__)
        try:
            db.query(
                "INSERT INTO audit_logs (actor, action, detail) VALUES (%s, %s, %s)",
                (
                    "origination-service",
                    "kyc_unavailable",
                    f"app_id={app_id} error={type(e).__name__}",
                ),
            )
        except Exception as audit_err:  # noqa
            # Audit write is best-effort — never 500 intake on it, but make the miss loud.
            log.error(
                "failed to record kyc_unavailable audit for app_id=%s: %s",
                app_id,
                type(audit_err).__name__,
            )
    return {
        "app_id": app_id,
        "status": "submitted",
        "kyc": KycOut(**cip),
        "kyc_checked": kyc_checked,
    }


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


@router.post("/{app_id}/monthly-debt")
def capture_monthly_debt(
    app_id: int,
    body: MonthlyDebtIn,
    x_internal_service: str | None = Header(default=None, alias="X-Internal-Service"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
):
    """Capture monthly_debt for an existing application.

    Remediation path for the decisioning quarantine: a legacy/seeded row with NULL
    monthly_debt is rejected with 422 at decisioning; this records the value so the
    application becomes decisionable, rather than leaving manual SQL as the only fix.

    Internal-only (PR review): monthly_debt feeds the model, and the endpoint is
    otherwise reachable through the gateway's anonymous /los proxy, so an external
    caller who knows a legacy/NULL app id could inject the underwriting input. It now
    requires the X-Internal-Service shared secret — which the gateway strips from
    external requests — so only a server-side/ops caller holding the token can amend a
    row. There is no automated caller; this is a deliberate operator escape hatch.

    Capture-only, never overwrite: the UPDATE is guarded by `monthly_debt IS NULL`,
    matching this endpoint's purpose as a NULL-row quarantine escape hatch. An already
    recorded value is frozen — 409, not a silent overwrite. The UPDATE uses RETURNING
    and a zero-row result is a 409 too: it means a concurrent capture set the value
    between our existence check and the write (the race PR review flagged), so we must
    NOT return the unpersisted value as success.

    Every capture writes an audit_logs row (actor from X-User-Id when the caller
    supplies it, else the service identity) so the amendment is attributable.
    """
    _require_internal_caller(x_internal_service)
    existing = db.query(
        "SELECT monthly_debt FROM applications WHERE id = %s", (app_id,)
    )
    if not existing:
        raise HTTPException(status_code=404, detail="application not found")
    if existing[0]["monthly_debt"] is not None:
        raise HTTPException(
            status_code=409,
            detail="monthly_debt is already recorded for this application",
        )
    updated = db.query(
        "UPDATE applications SET monthly_debt = %s WHERE id = %s AND monthly_debt IS NULL "
        "RETURNING id",
        (body.monthly_debt, app_id),
    )
    if not updated:
        # Lost the race: a concurrent capture set monthly_debt between the check above
        # and this write. Report the conflict, never a 200 with a value we did not
        # persist (PR review).
        raise HTTPException(
            status_code=409,
            detail="monthly_debt is already recorded for this application",
        )
    db.query(
        "INSERT INTO audit_logs (actor, action, detail) VALUES (%s, %s, %s)",
        (
            x_user_id or "internal-service",
            "capture_monthly_debt",
            f"app_id={app_id} monthly_debt={body.monthly_debt}",
        ),
    )
    return {"app_id": app_id, "monthly_debt": body.monthly_debt}


def decision_request_payload(app_id: int) -> dict:
    """Build the decision-service request for an application from the LOS database.

    Also the assistant's score tool (app/assistant.py): applicant data is looked up
    here by code — the model supplies only an application id, never applicant fields.
    Returns None when the application does not exist.
    """
    rows = db.query(
        "SELECT a.id, a.applicant_id, a.amount, a.term_months, a.income, "
        "a.monthly_debt, a.employment_years, ap.name, ap.ssn "
        "FROM applications a LEFT JOIN applicants ap ON ap.id = a.applicant_id WHERE a.id = %s",
        (app_id,),
    )
    if not rows:
        return None
    r = rows[0]
    if r.get("monthly_debt") is None:
        # Fail closed (PR #7 review): a persisted application with no recorded
        # monthly_debt must NOT be decisioned as if the applicant were debt-free — that
        # silently reintroduces the over-approval risk and persists monthly_debt: 0 into
        # the append-only decision event, making the bad input look intentional. New API
        # rows always carry it (ApplicationIn requires it); legacy / seeded / non-API
        # rows with NULL are quarantined here until the value is captured, never
        # defaulted to 0.
        raise HTTPException(
            status_code=422,
            detail=(
                "monthly_debt is not recorded for this application; it must be "
                "captured before a decision can be made"
            ),
        )
    return {
        "application_id": app_id,
        "applicant_id": r.get("applicant_id"),
        "name": r.get("name"),
        "ssn": r.get("ssn") or "",
        "requested_amount": r.get("amount"),
        "term_months": r.get("term_months"),
        "annual_income": r.get("income") or 0,
        "monthly_debt": r.get("monthly_debt"),  # guaranteed non-NULL by the guard above
        "employment_years": r.get("employment_years") or 0,
        "credit_score": None,  # pulled downstream by decision-service
    }


@router.post("/{app_id}/decision", response_model=DecisionOut)
def run_decision(
    app_id: int,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    # Optional Idempotency-Key header is forwarded as the decision-service request_id:
    # a retry after a timeout on this officer path replays the recorded decision instead
    # of re-pulling credit and appending a second regulated event (PR #7 review).
    if idempotency_key is not None and len(idempotency_key) > 64:
        raise HTTPException(
            status_code=400, detail="Idempotency-Key must be at most 64 characters"
        )
    payload = decision_request_payload(app_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="application not found")
    if idempotency_key:
        payload["request_id"] = idempotency_key
    # Decisioning moved to decision-service; it persists the decision_events record.
    try:
        resp = clients.post(clients.DECISION_URL, "/decisions", payload)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else 503
        if status == 409:
            # Reused idempotency key with changed inputs: surface the conflict, not a
            # generic unavailability, so the caller does not blindly retry.
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key reused with different decision inputs",
            ) from exc
        # decision-service fails closed with a 503 on bureau/record/unmapped-feature
        # refusals — surface that as a retryable decisioning-unavailable, not a LOS 500,
        # so officers and monitoring see the fail-closed reason class (matches the
        # assistant route's handling).
        log.error("decision-service refused decision for app_id=%s: %s", app_id, exc)
        raise HTTPException(status_code=503, detail="decisioning unavailable") from exc
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
