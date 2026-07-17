"""Offer / Truth-in-Lending disclosure generation.

The offer build + APR/finance-charge + amortization logic was extracted into
disclosure-service. This router is now a thin pass-through: it calls disclosure-service
over HTTP and maps its response into the OfferOut shape the frontend already expects.
"""

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .. import authz, clients, db
from ..schemas import Disclosure, OfferOut, ScheduleRow

router = APIRouter(tags=["offers"])

# Server-side offer rate (was a frontend constant, OFFER_RATE_PCT). The disclosure rate
# is policy, not caller input; a risk-based rate derived from the decision is future work.
POLICY_RATE_PCT = 7.99


class OfferIn(BaseModel):
    # Only the application id is accepted. Loan terms (principal/rate/term) are bound from
    # the stored application, never the caller (see make_offer, PR review).
    app_id: int


def _to_offer_out(app_id: int, resp: dict) -> OfferOut:
    """Map a disclosure-service OfferResponse into the LOS OfferOut/Disclosure shape."""
    d = resp.get("disclosure") or {}
    rows = resp.get("schedule") or d.get("schedule") or []
    disclosure = Disclosure(
        apr=d.get("apr", 0),
        finance_charge=d.get("finance_charge", 0),
        monthly_payment=d.get("monthly_payment", 0),
        amount_financed=d.get("amount_financed", 0),
        total_of_payments=d.get("total_of_payments", 0),
        schedule=[ScheduleRow(**row) for row in rows],
    )
    return OfferOut(app_id=app_id, disclosure=disclosure)


@router.post("/offer", response_model=OfferOut)
def make_offer(
    body: OfferIn,
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
):
    # ADR 0010: generating a TILA offer persists a disclosure for the application, so only
    # an officer or the owning borrower may do it -- never an anonymous /los caller who
    # guessed the id (the confused-deputy write this closes).
    authz.require_officer_or_owner(body.app_id, x_user_role, x_user_id)
    # Bind the disclosure inputs to the STORED application, never the caller (PR review):
    # /los/offer is reachable anonymously through the gateway, and origination forwards the
    # internal-service token to disclosure-service, so accepting caller-supplied
    # principal/rate/term made this a confused deputy — an external caller could write a
    # fabricated TILA offer (persisted, and later read by accept_offer to board the loan)
    # for any guessed app id, bypassing disclosure-service's internal-only guard. Look the
    # loan terms up by app_id and apply a server-side policy rate. Mirrors
    # decision_request_payload. (The remaining anonymous-trigger IDOR is ADR 0010.)
    rows = db.query(
        "SELECT amount, term_months FROM applications WHERE id = %s", (body.app_id,)
    )
    if not rows:
        raise HTTPException(status_code=404, detail="application not found")
    app_row = rows[0]
    # Decision-state guard (PR review, ADR 0010 alt 3 defense-in-depth): a TILA offer is
    # only meaningful for an APPROVED application, so gate on the latest decision — an
    # offer must not be generated/persisted for a denied/referred/undecided app. This
    # matches the UI, which only shows the offer CTA on approve, and limits the blast
    # radius of the anonymous trigger. (Authorization — WHOSE application this is — is the
    # separate officer-OR-owner check deferred to ADR 0010.)
    decision_rows = db.query(
        "SELECT outcome FROM decisions WHERE app_id = %s", (body.app_id,)
    )
    if (
        not decision_rows
        or (decision_rows[0].get("outcome") or "").lower() != "approve"
    ):
        raise HTTPException(
            status_code=409, detail="offer requires an approved decision"
        )
    resp = clients.post(
        clients.DISCLOSURE_URL,
        "/offers",
        {
            "application_id": body.app_id,
            "principal": app_row["amount"],
            "term_months": app_row["term_months"],
            "annual_rate": POLICY_RATE_PCT,
        },
    )
    return _to_offer_out(body.app_id, resp)


@router.get("/applications/{app_id}/offer", response_model=OfferOut)
def get_offer(
    app_id: int,
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
):
    # ADR 0010: the offer discloses APR/terms for the application -- officer or owner only.
    authz.require_officer_or_owner(app_id, x_user_role, x_user_id)
    resp = clients.get(clients.DISCLOSURE_URL, f"/applications/{app_id}/offer")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="no offer for this application")
    resp.raise_for_status()
    return _to_offer_out(app_id, resp.json())
