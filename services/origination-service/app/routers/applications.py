"""Application intake, listing, detail, decisioning, and acceptance/boarding."""

import hmac

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
def submit_application(body: ApplicationIn):
    payload = body.model_dump()
    # ADR 0010 Phase B: create_application persists the applicant+application AND its
    # continuation token in one INSERT and returns both (PR review). The token is the
    # anonymous applicant's only credential to complete decision/offer/accept, so it is
    # atomic with the application row -- if the write fails, submit fails, never a durable
    # application with a NULL token and no recovery path. Returned once below; the frontend
    # carries it as X-Application-Token.
    app_id, continuation_token = intake.create_application(payload)
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
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_application_token: str | None = Header(default=None, alias="X-Application-Token"),
):
    # ADR 0010: decisioning pulls credit and appends a regulated decision event, so only an
    # officer, the owning borrower, or the applicant holding this application's
    # continuation token may trigger it -- never an anonymous caller who guessed the id.
    authz.require_officer_or_owner(app_id, x_user_role, x_user_id, x_application_token)
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
    return DecisionOut(
        app_id=app_id,
        decision=resp["outcome"],
        score=int(round(resp.get("score") or 0)),  # DecisionOut.score is int
        adverse_action_reason=resp.get("reason"),
    )


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
    authz.require_officer_or_owner(app_id, x_user_role, x_user_id, x_application_token)
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
        "SELECT a.amount, a.term_months, ap.name, o.apr, d.outcome "
        "FROM applications a "
        "LEFT JOIN applicants ap ON ap.id = a.applicant_id "
        "LEFT JOIN decisions d ON d.app_id = a.id "
        "LEFT JOIN offers o ON o.app_id = a.id "
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
        try:
            loan_id = intake.board_to_servicing(
                app_id,
                r.get("name") or "Borrower",
                r["amount"],
                r["apr"],
                r["term_months"],
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
    # Clearing the hash + expiry makes the token single-use at funding -- a token left in
    # browser storage / shared-device residue cannot re-drive a funded application. Idempotent
    # on replay (already NULL). Officer/owner access is unaffected (it never used the token).
    db.query(
        "UPDATE applications SET status = 'funded', continuation_token = NULL, "
        "continuation_token_expires_at = NULL WHERE id = %s",
        (app_id,),
    )
    return {"loan_id": loan_id}
