"""Application intake, listing, detail, decisioning, and acceptance/boarding."""

import hmac
import re

import httpx
from psycopg2 import errors as pg_errors
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import authz, clients, config, db, intake, kyc_gate, models
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
    PrincipalReason,
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


def _run_kyc(
    app_id: int,
    applicant_id: int | None,
    name: str | None,
    dob: str | None,
    ssn: str | None,
    address: str | None,
    is_entity: bool,
) -> tuple[dict, bool]:
    """Call kyc-service for an application and map the result to the four KycOut booleans.

    kyc-service persists its own kyc_checks row (the authoritative gate, ADR 0011). Returns
    (cip, kyc_checked): cip is the boolean map for the response; kyc_checked is False when
    the call did NOT complete (outage/timeout/auth failure/persistence 503) -- distinct from
    a KYC that ran and declined (200 with cip_passed False). A failure records a
    kyc_unavailable audit row and never 500s the caller (deliberate intake resilience); the
    application then has no kyc_checks row and stays blocked at the gate until a successful
    recheck persists one (see recheck_kyc). Shared by submit and recheck so the mapping and
    the failure handling cannot drift on this regulated path.
    """
    cip = {
        "name_verified": False,
        "dob_verified": False,
        "address_verified": False,
        "ssn_verified": False,
    }
    kyc_checked = True
    try:
        resp = clients.post(
            clients.KYC_URL,
            "/kyc/check",
            {
                "application_id": app_id,
                "applicant_id": applicant_id,
                "name": name,
                "dob": dob,
                "ssn": ssn,
                "address": address,
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
        # A transport/auth failure (outage, timeout, a missing/rotated internal token ->
        # 403, or a persistence 503) is NOT a KYC "not verified" result -- the check never
        # persisted. A genuine decline comes back 200 with cip_passed False, so only an
        # exception reaches here. Keep the deliberate intake resilience (a KYC hiccup must
        # not 500 the applicant), but do NOT let the failure masquerade as an ordinary
        # all-false verification: raise the log to error, record a kyc_unavailable audit
        # row so an application created while KYC was down is queryable, and flag
        # kyc_checked=False so a caller can tell it apart from a real decline.
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
    return cip, kyc_checked


@router.post("", response_model=ApplicationCreated)
def submit_application(
    body: ApplicationIn,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
):
    payload = body.model_dump()
    # ADR 0010 Phase B: create_application persists the applicant+application AND its
    # continuation token in one INSERT and returns both (PR review). The token is the
    # anonymous applicant's only credential to complete decision/offer/accept, so it is
    # atomic with the application row -- if the write fails, submit fails, never a durable
    # application with a NULL token and no recovery path. Returned once below; the frontend
    # carries it as X-Application-Token.
    #
    # D24 (PR #38 review): the gateway forwards X-User-Id for any session-bearing request,
    # including this anonymous-by-default route, so a LOGGED-IN caller (e.g. an officer
    # applying under their own information) is recorded as the submitter. An unauthenticated
    # apply carries no header and stays None -- the ordinary anonymous case, not a gap.
    try:
        submitted_by_user_id = int(x_user_id) if x_user_id is not None else None
    except ValueError:
        submitted_by_user_id = None
    app_id, continuation_token = intake.create_application(
        payload, submitted_by_user_id
    )
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
    # A kyc-service hiccup must not 500 the intake (resilience kept); on failure the app is
    # recoverable via POST /applications/{app_id}/recheck-kyc without resubmitting.
    is_entity = bool(payload.get("is_entity"))
    cip, kyc_checked = _run_kyc(
        app_id,
        applicant_id,
        payload.get("name"),
        payload.get("dob"),
        payload.get("ssn"),
        payload.get("address"),
        is_entity,
    )
    return {
        "app_id": app_id,
        "status": "submitted",
        "kyc": KycOut(**cip),
        "kyc_checked": kyc_checked,
        "continuation_token": continuation_token,
    }


@router.post("/{app_id}/recheck-kyc", response_model=ApplicationCreated)
def recheck_kyc(
    app_id: int,
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_application_token: str | None = Header(default=None, alias="X-Application-Token"),
):
    # In-product recovery for an application submitted while kyc-service was unavailable
    # (PR review). Under the mandatory persisted-KYC gate (ADR 0011) such an application
    # has no kyc_checks row and cannot decision/offer/board; before this route the only
    # recourse was resubmitting, which created a duplicate applicant/application. This
    # re-runs KYC for the existing application from its stored identity fields and lets
    # kyc-service persist the row, repairing the original. Same officer-OR-owner-OR-token
    # authorization as the other application-scoped routes (ADR 0010).
    authz.require_officer_or_owner(app_id, x_user_role, x_user_id, x_application_token)
    rows = db.query(
        "SELECT a.applicant_id, ap.name, ap.dob, ap.ssn, ap.address, ap.is_entity "
        "FROM applications a JOIN applicants ap ON ap.id = a.applicant_id "
        "WHERE a.id = %s",
        (app_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="application not found")
    r = rows[0]
    # dob is a DATE column -> a date object; kyc-service CipCheckIn expects an optional
    # string, so serialize it (None stays None for an entity/partial applicant).
    dob = r["dob"].isoformat() if r.get("dob") else None
    cip, kyc_checked = _run_kyc(
        app_id,
        r["applicant_id"],
        r["name"],
        dob,
        r["ssn"],
        r["address"],
        bool(r["is_entity"]),
    )
    return {
        "app_id": app_id,
        "status": "submitted",
        "kyc": KycOut(**cip),
        "kyc_checked": kyc_checked,
        # Echo back the token the caller authenticated with so a client that stores this
        # ApplicationCreated response does not null its own capability (PR review). authz
        # already validated it against the stored token, so this discloses nothing new; it
        # is the credential the anonymous applicant needs for the next decision/offer/accept.
        # None for officer/owner callers (session-authed) -- they never use the token path.
        "continuation_token": x_application_token,
    }


@router.get("", response_model=Page[ApplicationListItem])
def list_applications(
    session: Session = Depends(get_session),
    status: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
):
    # ADR 0010: the list dumps applicant PII across every application, so it is an
    # officer-only view -- a borrower reads their own application by id, never the roster,
    # and an anonymous /los caller must not enumerate the book of business.
    authz.require_officer(x_user_role)
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


def _normalize_principal_reasons(raw: object) -> list[PrincipalReason]:
    """Allowlist a decision_events.principal_reasons JSONB value to {code, reason, feature}.

    That column is unconstrained JSONB, written only by decision-service's reasons.py
    but readable by any legacy/backfill/hand-edit, and reaches borrower-readable
    responses both on GET detail and on the POST decision path (idempotency replay
    rebuilds it from the same persisted row) -- both must sanitize identically
    (Codex review). An item with no non-empty `reason` string is dropped entirely,
    not kept with `reason=None`: get_application derives the legacy single-string
    adverse_action_reason from the FIRST normalized item, so a leading code-only row
    (e.g. a legacy/backfilled `{"code": "R02"}`) would otherwise silently suppress the
    borrower's denial explanation even when a later item carries real text (Codex
    review, PR 34).
    """
    items = raw if isinstance(raw, list) else []
    normalized = [
        PrincipalReason(
            **{
                k: v
                for k, v in item.items()
                if k in ("code", "reason", "feature") and isinstance(v, str)
            }
        )
        for item in items
        if isinstance(item, dict)
    ]
    return [r for r in normalized if r.reason]


@router.get("/{app_id}", response_model=ApplicationDetail)
def get_application(
    app_id: int,
    session: Session = Depends(get_session),
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_application_token: str | None = Header(default=None, alias="X-Application-Token"),
):
    # ADR 0010: the detail view exposes applicant PII (name, SSN-bearing applicant row,
    # decision, offer), so only an officer, the owning borrower, or the applicant holding
    # this application's continuation token may read it. Closes the anonymous serial-id PII
    # enumeration the /los proxy otherwise allows.
    authz.require_officer_or_owner(app_id, x_user_role, x_user_id, x_application_token)
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
    # `decisions` is outcome-only (models.Decision, debt D4); the score and Reg B principal
    # reasons live on the append-only `decision_events` row (ADR 0009). Without this, resuming
    # a denied application, or an officer opening one without rerunning decisioning, showed
    # "denied" with no reasons (PR review). Latest event wins, mirroring
    # disclosure_coordinator.gather_disclosure_context's own read of this table.
    #
    # `outcome` is selected from decision_events here too, not read off `dec` above (Codex
    # review): decision-service writes decision_events and decisions in ONE atomic statement
    # (decision-service/app/decision.py::_persist_event), so the two tables are never
    # inconsistent AT WRITE TIME -- but `dec` and this query are two separate reads on two
    # separate connections with no shared snapshot, and an officer can redecide with no
    # idempotency key at any time. A redecision landing between the two reads paired a stale
    # `dec.outcome` with the NEW event's score/reasons -- a regulated, borrower-visible
    # mismatch (e.g. showing "denied" reasons for an application that was just approved).
    # Sourcing outcome from the same row as the reasons/score closes that window: this
    # response is now internally consistent by construction, whichever event the query
    # happens to land on. `dec.outcome` remains only as the fallback for a legacy row with
    # `decisions.outcome` set but no matching decision_events (seed apps 6012/6013) -- a case
    # with no reasons to mismatch against.
    events = db.query(
        "SELECT outcome, principal_reasons, drivers FROM decision_events WHERE app_id = %s "
        "ORDER BY decided_at DESC, id DESC LIMIT 1",
        (app_id,),
    )
    latest_event = events[0] if events else None
    decision_outcome = (
        latest_event["outcome"] if latest_event else (dec.outcome if dec else None)
    )
    # Normalize by TYPE, not just truthiness (teeth review round 2): `x or []` passes a
    # non-list/non-dict JSONB value straight through (an object where a list was expected,
    # or vice versa), and `principal_reasons[0]` / `drivers.get(...)` below would then
    # raise on the container itself, before the guard on individual reason items even runs.
    raw_reasons = latest_event["principal_reasons"] if latest_event else None
    raw_drivers = latest_event["drivers"] if latest_event else None
    drivers = raw_drivers if isinstance(raw_drivers, dict) else {}
    principal_reasons = _normalize_principal_reasons(raw_reasons)
    adverse_action_reason = principal_reasons[0].reason if principal_reasons else None
    # model_score is likewise unvalidated JSONB (Codex review): a string, dict, or bool
    # must degrade to no score, not raise out of round().
    raw_score = drivers.get("model_score")
    model_score = (
        raw_score
        if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool)
        else None
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
        decision=decision_outcome,
        score=int(round(model_score)) if model_score is not None else None,
        adverse_action_reason=adverse_action_reason,
        principal_reasons=principal_reasons,
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


@router.post("/{app_id}/abandon")
def abandon_application(
    app_id: int,
    x_internal_service: str | None = Header(default=None, alias="X-Internal-Service"),
):
    """Delete an INERT just-submitted application (compensating rollback, PR #7 review).

    The gateway calls this when it cannot store the anonymous resume session after
    origination has already committed the application (a Redis failure in the submit window).
    Without it, that application is stranded: the applicant never got a working credential and
    will resubmit, leaving a duplicate PII-bearing row for manual cleanup. Deleting it makes
    submit atomic from the applicant's perspective -- the retry creates one clean application,
    not a duplicate-plus-orphan.

    Internal-only (X-Internal-Service; the gateway strips any client copy) -- deletion is an
    ops/compensation action, never client-reachable through the anonymous /los proxy.

    Guarded: only an INERT application is deletable -- no decision, no offer, no boarded loan.
    If any exists the application is past the submit window and is NOT deleted (409); this can
    never remove a decisioned/funded application. Deletes the application, then its applicant
    if no other application references it (intake creates one applicant per application).
    """
    _require_internal_caller(x_internal_service)
    rows = db.query(
        "SELECT a.applicant_id, "
        "  (SELECT count(*) FROM decisions d WHERE d.app_id = a.id) AS n_decisions, "
        "  (SELECT count(*) FROM offers o WHERE o.app_id = a.id) AS n_offers, "
        "  (SELECT count(*) FROM loans l WHERE l.app_id = a.id) AS n_loans "
        "FROM applications a WHERE a.id = %s",
        (app_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="application not found")
    r = rows[0]
    if r["n_decisions"] or r["n_offers"] or r["n_loans"]:
        # Past the submit window -- a real in-flight/funded application. Never delete it.
        raise HTTPException(
            status_code=409, detail="application is not inert; refusing to abandon"
        )
    applicant_id = r["applicant_id"]
    # Atomic (PR #7 review): submit runs KYC before the resume session is stored, so a just-
    # submitted application usually already has a kyc_checks row keyed to applicant_id. That
    # FK has no ON DELETE CASCADE, so deleting the applicant would fail AFTER the application
    # delete had committed under db.query's per-statement autocommit -- stranding the applicant
    # plus its KYC/PII rows, the exact orphan this endpoint exists to prevent. Do the whole
    # delete in one transaction, removing dependent rows (kyc_checks) before the applicant.
    try:
        with db.transaction() as cur:
            cur.execute("DELETE FROM applications WHERE id = %s", (app_id,))
            if applicant_id is not None:
                cur.execute(
                    "SELECT 1 FROM applications WHERE applicant_id = %s LIMIT 1",
                    (applicant_id,),
                )
                if cur.fetchone() is None:
                    # No other application references this applicant (intake creates one
                    # applicant per application). Remove the applicant's dependent PII rows,
                    # then the applicant. kyc_checks.applicant_id -> applicants(id) has no
                    # cascade, so this explicit child delete is what lets the applicant delete
                    # succeed.
                    cur.execute(
                        "DELETE FROM kyc_checks WHERE applicant_id = %s",
                        (applicant_id,),
                    )
                    cur.execute("DELETE FROM applicants WHERE id = %s", (applicant_id,))
    except pg_errors.ForeignKeyViolation:
        # TOCTOU (PR #7 review): the inertness probe above ran outside this transaction, so a
        # decision/offer/loan could race in between the probe and the DELETE. It cannot cause a
        # wrongful delete -- decisions/decision_events/offers carry RESTRICT FKs to
        # applications (no ON DELETE CASCADE), and any non-inert state requires a prior decision
        # (offer generation and boarding are gated on an approved decision) -- so the DELETE
        # aborts the whole transaction rather than removing a now-non-inert application. The FK,
        # not the probe timing, is the hard guarantee; surface the raced case as 409 (same as
        # the up-front guard) instead of a 500.
        raise HTTPException(
            status_code=409, detail="application is not inert; refusing to abandon"
        )
    log.info(
        "abandoned inert application app_id=%s (resume-session compensation)", app_id
    )
    return {"app_id": app_id, "status": "abandoned"}


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


def summary_payload(app_id: int) -> dict | None:
    """Build the officer loan-summary payload for the `loan_application_summary` prompt.

    Advisory (no record is written), so unlike `decision_request_payload` this NEVER
    raises on a NULL field — it omits the key and lets the prompt's "summarize ONLY facts
    present" rule handle the gap. Returns None when the application does not exist.

    Selects ONLY non-identity columns and NEVER joins `applicants`: name/dob/ssn/address
    are unreachable by construction, not merely filtered (the prompt's `json_vars` redaction
    stays defense-in-depth, not the control). `offers` is at most one row per app
    (`uq_offers_app`); `kyc_checks` can repeat, so take the latest by id.
    """
    rows = db.query(
        "SELECT a.amount, a.term_months, a.purpose, a.income, a.monthly_debt, "
        "a.employer, a.job_title, a.employment_years, a.status, "
        "o.apr, o.finance_charge, o.monthly_payment, o.amount_financed, o.total_of_payments, "
        "d.outcome, "
        "k.name_verified, k.dob_verified, k.address_verified, k.ssn_verified "
        "FROM applications a "
        "LEFT JOIN offers o ON o.app_id = a.id "
        "LEFT JOIN decisions d ON d.app_id = a.id "
        "LEFT JOIN LATERAL ("
        "  SELECT name_verified, dob_verified, address_verified, ssn_verified "
        "  FROM kyc_checks WHERE applicant_id = a.applicant_id ORDER BY id DESC LIMIT 1"
        ") k ON TRUE "
        "WHERE a.id = %s",
        (app_id,),
    )
    if not rows:
        return None
    # Drop NULL fields: an advisory summary states only what is present (a missing offer,
    # decision, or KYC row leaves its keys absent rather than surfacing bare nulls).
    return {key: value for key, value in rows[0].items() if value is not None}


@router.post("/{app_id}/decision", response_model=DecisionOut)
def run_decision(
    app_id: int,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_application_token: str | None = Header(default=None, alias="X-Application-Token"),
):
    # ADR 0010: decisioning pulls credit and appends a regulated decision event, so only an
    # officer, the owning borrower, or the applicant holding this application's
    # continuation token may trigger it -- never an anonymous caller who guessed the id.
    authz.require_officer_or_owner(app_id, x_user_role, x_user_id, x_application_token)
    # Client ask (2026-08-12 governance §5): an officer may not decision an application
    # where their own account is the applicant. Refuses an already-authorized caller, so
    # it runs after the ADR 0010 gate and before the credit pull -- a blocked attempt
    # appends no decision event. Borrowers are untouched (see deny_self_decision).
    authz.deny_self_decision(app_id, x_user_role, x_user_id)
    # ADR 0011: a credit pull is a regulated action -- require a passing KYC first (fails
    # closed on a declined or never-run check), so a failed/absent identity check can never
    # reach decisioning or, transitively, funding.
    kyc_gate.require_kyc_passed(app_id)
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
    # score is unvalidated downstream JSON, same as get_application's drivers.model_score
    # (Codex review): decision-service can rebuild this response from persisted
    # decision_events on idempotency replay, so a nonnumeric value must degrade to no
    # score instead of raising out of round() or `or 0` fabricating a zero.
    raw_score = resp.get("score")
    score = (
        int(round(raw_score))
        if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool)
        else None
    )
    return DecisionOut(
        app_id=app_id,
        decision=resp["outcome"],
        score=score,
        adverse_action_reason=resp.get("reason"),
        # `reason` is decision-service's FIRST principal reason only; the full ranked set
        # is `principal_reasons`. Forward both — the screens read the list, and callers
        # already reading the single field keep working. decision-service can rebuild
        # this list from the persisted decision_events row on idempotency replay, so it
        # carries the same unconstrained-JSONB risk as the GET detail route's read of
        # that table -- normalize through the same allowlist (Codex review, PR 34).
        principal_reasons=_normalize_principal_reasons(resp.get("principal_reasons")),
    )


# A plain-decimal literal, so a positive rate parses without Decimal's exotic accepts
# (underscores, signs, scientific notation) reaching the servicing rate column.
_PLAIN_DECIMAL = re.compile(r"^\d+(\.\d+)?$")


def _note_rate_for_boarding(snapshot, apr: float) -> float:
    """The contractual note rate to board, derived from the delivered disclosure's snapshot.

    Servicing amortizes at this rate; boarding the actuarial APR instead made the funded
    loan's schedule contradict its own TILA disclosure on every fee-bearing loan. Read from
    the stored `compute_snapshot` — the authoritative record of what the disclosed figures
    were derived from — never from a caller. Boarding already requires a delivered disclosure
    with a recorded document, so a boardable loan always has this snapshot; fail closed (409)
    rather than fall back to the APR when it is absent or unusable, because boarding at the
    wrong rate is the defect this exists to prevent.
    """
    note_rate_pct = (
        snapshot.get("note_rate_pct") if isinstance(snapshot, dict) else None
    )
    if note_rate_pct is None or not _PLAIN_DECIMAL.match(str(note_rate_pct)):
        raise HTTPException(
            status_code=409,
            detail=(
                "the delivered TILA disclosure for this offer has no usable note rate; "
                "refusing to board a loan whose servicing rate cannot be determined"
            ),
        )
    note_rate = float(note_rate_pct)
    # Spec D1 invariant: the disclosed APR is >= the note rate for every non-zero-fee loan,
    # because the APR annualizes the prepaid fee on top of the note rate. A snapshot note
    # rate above the offer's APR means the two records disagree — fail closed rather than
    # board either.
    if note_rate > apr:
        raise HTTPException(
            status_code=409,
            detail=(
                "the disclosure note rate exceeds the offer APR; the offer and its "
                "disclosure disagree, refusing to board"
            ),
        )
    return note_rate


@router.post("/{app_id}/accept")
def accept_offer(
    app_id: int,
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_application_token: str | None = Header(default=None, alias="X-Application-Token"),
):
    # ADR 0010: acceptance boards a real loan (loans + balances, status=funded), the
    # money-moving action, so only an officer, the owning borrower, or the applicant
    # holding this application's continuation token may accept -- never an anonymous caller
    # who guessed an approved application id.
    try:
        authz.require_officer_or_owner(
            app_id, x_user_role, x_user_id, x_application_token
        )
    except HTTPException:
        # Terminal accept replay (PR review): the FIRST accept funds the loan and retires
        # the continuation token (expiry nulled below), so a lost-response retry from the
        # anonymous applicant -- whose token is their only credential -- would now 404 and
        # hide a funded loan behind an unrecoverable failure. If the retried token still
        # matches THIS application's preserved token hash AND a loan is already boarded,
        # replay that loan. This path only RETURNS an existing loan (never boards, never
        # touches state), so the retired token grants no new capability -- forward routes
        # (decision/offer/detail) stay closed because they see the nulled expiry as expired.
        replay_loan = authz.terminal_accept_replay(app_id, x_application_token)
        if replay_loan is None:
            raise
        return {"loan_id": replay_loan}
    # ADR 0011: boarding is the money action -- require a passing KYC (defense in depth;
    # decisioning is already gated, but never board a funded loan on an unverified identity
    # even if an approved decision somehow predates the gate).
    kyc_gate.require_kyc_passed(app_id)
    # Decision-state guard (PR review, ADR 0010 alt 3 defense-in-depth): boarding creates
    # a real loan (loans + balances, status=funded), so require the application to have an
    # APPROVED decision AND a generated offer before boarding — never rely on the UI to
    # gate it, and never board at a default rate when no offer exists. (Authorization —
    # whose application this is — is the separate officer-OR-owner check in ADR 0010.)
    rows = db.query(
        "SELECT a.amount, a.term_months, ap.name, o.apr, d.outcome, "
        "ds.status AS disclosure_status, ds.delivered_at AS disclosure_delivered_at, "
        # The note rate servicing must amortize at (spec D1: APR carries the fee and is
        # higher, so the disclosed schedule uses the note rate, not the APR). Recorded at
        # compute time in the disclosure's snapshot — the authoritative value the disclosed
        # figures were derived from — and read here so boarding stores it on the loan.
        "ds.compute_snapshot AS disclosure_snapshot, "
        # A flag, not the document: this path only needs to know one was persisted, and
        # hauling a regulated body through the boarding query would put it in a result the
        # money path has no use for.
        #
        # The `<> 'null'` half is not redundant. In Postgres a JSON null is a VALUE, so
        # `'null'::jsonb IS NOT NULL` is TRUE — a column holding it would read as a
        # recorded document while meaning the opposite. The write path only ever stores an
        # object or SQL NULL, so this covers a row shaped by hand-written SQL, on the same
        # posture as the delivered_at check below: corrupt is not a licence to board.
        "(ds.document_body IS NOT NULL AND ds.document_body <> 'null'::jsonb) "
        "AS disclosure_has_document, "
        # The two decision edges, to reject a split-brain provenance chain at the money step.
        # `o.decision_event_id` is the edge the provenance view walks (the offer's); the
        # regulated disclosure carries its OWN `ds.decision_event_id`, stamped and validated at
        # write. disclosure-service refuses to DELIVER when they diverge, but a row delivered
        # before that guard existed can carry the divergence over a frozen `delivered` row, so
        # boarding re-checks here on the money path's own query rather than trusting the flag.
        "o.decision_event_id AS offer_decision_event_id, "
        "ds.decision_event_id AS disclosure_decision_event_id, "
        # The outcome of the decision the REGULATED disclosure cites, read on the money
        # path's own query. disclosure-service now refuses to create a disclosure whose
        # decision_event_id did not approve, but a back-book row written before that guard
        # can carry a same-application deny/refer edge over a frozen `delivered` row that is
        # now unrepairable (trg_disclosures_freeze_delivered), so boarding re-checks here
        # rather than trust the create-time gate — same posture as the split-brain guard.
        "dde.outcome AS disclosure_decision_outcome "
        "FROM applications a "
        "LEFT JOIN applicants ap ON ap.id = a.applicant_id "
        "LEFT JOIN decisions d ON d.app_id = a.id "
        "LEFT JOIN offers o ON o.app_id = a.id "
        # Bound to THIS offer, not to the application: the disclosure that must have been
        # delivered is the one describing the very terms being boarded below. Joining by
        # app_id would let a delivered disclosure for some other offer authorize boarding
        # a different one. uq_disclosures_offer keeps this at most one row per offer.
        "LEFT JOIN disclosures ds ON ds.offer_id = o.id "
        # The disclosure's OWN decision edge, to read its outcome for the guard above.
        "LEFT JOIN decision_events dde ON dde.id = ds.decision_event_id "
        "WHERE a.id = %s ORDER BY o.id DESC",
        (app_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="application not found")
    r = rows[0]
    if (r.get("outcome") or "").lower() != "approve":
        raise HTTPException(
            status_code=409, detail="application is not approved for boarding"
        )
    if r.get("apr") is None:
        raise HTTPException(
            status_code=409, detail="no offer to accept for this application"
        )
    # Idempotent boarding (PR review): a double-click / timeout-retry / concurrent POST
    # must not board a second loan + balance for the same application. Return the existing
    # loan if one is already boarded, and rely on the uq_loans_app unique index to settle
    # the concurrent race — the loser catches UniqueViolation and replays the winner's
    # loan. The DB unique index is the AUTHORITATIVE guarantee that duplicates cannot be
    # boarded; the graceful UniqueViolation->replay is best-effort, because db.py shares a
    # single non-thread-safe autocommit connection (CLAUDE.md raw-psycopg2 seam) so a truly
    # concurrent loser may surface a connection-level error (500) instead — a retry then
    # heals via the existing-loan path, and no duplicate loan is ever created either way.
    existing = db.query(
        "SELECT id, principal FROM loans WHERE app_id = %s ORDER BY id LIMIT 1",
        (app_id,),
    )
    if existing:
        loan_id = existing[0]["id"]
        principal = existing[0]["principal"]
    else:
        # TILA/Reg-Z timing hold (PR review): boarding IS this system's consummation event,
        # and 1026.17(b) puts the disclosure before it. disclosure-service already refuses
        # to deliver once a loan exists (_refuse_if_already_consummated); without the
        # reciprocal guard here that hold is one-sided — accept early and the disclosure
        # can NEVER be delivered, leaving a funded loan with no authoritative TILA record.
        # Fail closed on anything short of delivered: absent, draft, in_review, approved.
        #
        # Placed on the boarding branch only. A replay that finds an already-boarded loan
        # must still return it (and run the reconcile below) — 409ing there would not
        # un-board the loan, it would just hide it and strand the balance/funded heal.
        #
        # Safe without a transaction despite the read and the INSERT being separate
        # autocommitted statements (raw-psycopg2 seam, CLAUDE.md): the predicate is
        # MONOTONE. `delivered` is terminal in the lifecycle whitelist and frozen by
        # trg_disclosures_freeze_delivered, which rejects any UPDATE or DELETE of a
        # delivered row (and trg_disclosures_no_truncate closes the wholesale end-run), so
        # a status read as delivered cannot regress before the INSERT lands. It also
        # settles the reverse ordering: accept cannot observe `delivered` until delivery
        # has committed, so delivery always precedes boarding. IF A RE-ISSUE/SUPERSEDE FLOW
        # EVER MAKES `delivered` REVERSIBLE (the DDL anticipates one), this guard becomes a
        # genuine TOCTOU and needs a single-statement or DB-level condition instead.
        if (
            r.get("disclosure_status") != "delivered"
            or r.get("disclosure_delivered_at") is None
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "no delivered TILA disclosure for this offer; the disclosure must be "
                    "delivered before the loan is boarded"
                ),
            )
        # `delivered` alone does not prove a document was ever recorded. Migration 0012
        # added `document_body` nullable and leaves ALREADY-DELIVERED rows at NULL, and
        # disclosure-service refuses to backfill them because
        # trg_disclosures_freeze_delivered makes a delivered row immutable — so a row
        # delivered on a volume that ran 0011 before 0012 carries the flag and the timestamp
        # over content that does not exist. Boarding on that pair funds a loan whose
        # borrower-facing TILA document nobody can produce, which is the same defect the
        # delivery guard closes from the other side, at the money-moving step.
        #
        # Monotone in the same direction as the status check above, so it needs no
        # transaction either: `document_body` is written before delivery and frozen at it, so
        # a document read as present cannot vanish before the INSERT lands.
        if not r.get("disclosure_has_document"):
            raise HTTPException(
                status_code=409,
                detail=(
                    "the delivered TILA disclosure for this offer has no recorded "
                    "document; refusing to board a loan whose disclosure cannot be read"
                ),
            )
        # Split-brain provenance guard (defense in depth, mirrors disclosure-service's
        # delivery refusal). Both edges non-null and unequal means the disclosure record and
        # its offer name DIFFERENT decisions: the audit trail points to a decision that did
        # not authorize the boarded terms. The delivery guard closes this for any row
        # delivered from now on; this closes the back-book row delivered before that guard,
        # which trg_disclosures_freeze_delivered now makes unrepairable. A NULL on either side
        # is a legacy partial chain, not a divergence, and stays out of the money step by the
        # delivered/document guards above rather than here.
        offer_edge = r.get("offer_decision_event_id")
        record_edge = r.get("disclosure_decision_event_id")
        if (
            offer_edge is not None
            and record_edge is not None
            and offer_edge != record_edge
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "the delivered TILA disclosure's decision edge does not match the "
                    "offer being boarded; refusing to board a split-brain provenance chain"
                ),
            )
        # Non-approving decision guard (defense in depth, mirrors disclosure-service's
        # create-path refusal). A disclosure whose OWN decision edge names a deny/refer
        # event is a regulated chain the provenance view still reports complete — boarding
        # such a loan funds it over an audit trail that says it was not approved. Gated on
        # the edge being present, exactly like the split-brain check: a NULL edge is a legacy
        # partial chain the delivered/document guards above already keep out of the money
        # step, not a divergence to judge here. `decisions.outcome` (checked at line 705) is
        # the mutable current-state rollup; this is the append-only event the record cites.
        if (
            record_edge is not None
            and (r.get("disclosure_decision_outcome") or "").lower() != "approve"
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "the delivered TILA disclosure cites a non-approving decision; "
                    "refusing to board a loan over an unapproved provenance chain"
                ),
            )
        # Servicing amortizes at the NOTE rate, not the APR (spec D1): derive it from the
        # delivered disclosure's snapshot and fail closed if it is absent, so a loan is never
        # boarded at the disclosed APR — the rate the disclosed schedule was NOT built at.
        note_rate = _note_rate_for_boarding(r.get("disclosure_snapshot"), r["apr"])
        try:
            loan_id = intake.board_to_servicing(
                app_id,
                r.get("name") or "Borrower",
                r["amount"],
                r["apr"],
                r["term_months"],
                note_rate_pct=note_rate,
            )
            principal = r["amount"]  # what we just boarded with
        except pg_errors.UniqueViolation:
            # A concurrent acceptance won the race and boarded first; serve its loan
            # instead of a second one (one loan per app_id, enforced by uq_loans_app).
            won = db.query(
                "SELECT id, principal FROM loans WHERE app_id = %s ORDER BY id LIMIT 1",
                (app_id,),
            )
            if not won:
                raise HTTPException(
                    status_code=409,
                    detail="boarding conflict without a retrievable loan",
                )
            loan_id = won[0]["id"]
            principal = won[0]["principal"]
    # Reconcile servicing + LOS state on EVERY path, including replays (PR review). The
    # loan insert, balance insert, and status update are three separate autocommitted
    # statements (shared-connection psycopg2 seam, CLAUDE.md), so a first attempt could
    # board the loan then crash before the balance or the funded update — leaving a
    # durable loan with stale LOS state that a bare replay ("return existing") would never
    # heal. Both writes are idempotent (ON CONFLICT / set-to-funded), so re-running them
    # here self-heals that window on the next accept. The balance is reconciled from the
    # BOARDED loan's own principal (never the request/application amount) so a
    # missing-balance heal can never write a value that diverges from the loan. (Full
    # one-transaction atomicity is bounded by that raw-psycopg2 money-write seam — debt.)
    db.query(
        "INSERT INTO balances (loan_id, balance) VALUES (%s, %s) "
        "ON CONFLICT (loan_id) DO NOTHING",
        (loan_id, float(principal)),
    )
    # Fund AND retire the continuation token in one statement (PR #7 review): boarding is
    # the terminal money action, so the anonymous bearer capability must not outlive it.
    # Nulling the EXPIRY retires the token for every normal route -- authz treats a NULL
    # expiry as expired (_expired), so a token left in browser storage / shared-device
    # residue cannot re-drive a funded application (decision/offer/detail all deny). The
    # token HASH is deliberately PRESERVED (not nulled) so a lost-response accept retry can
    # still be verified for the replay-only terminal_accept_replay path above -- that path
    # can only return the already-boarded loan, never board or drive anything. Idempotent on
    # replay (expiry already NULL). Officer/owner access is unaffected (it never used the token).
    db.query(
        "UPDATE applications SET status = 'funded', "
        "continuation_token_expires_at = NULL WHERE id = %s",
        (app_id,),
    )
    return {"loan_id": loan_id}
