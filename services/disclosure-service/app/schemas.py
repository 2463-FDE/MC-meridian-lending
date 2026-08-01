"""Pydantic request/response models for the disclosure API."""

from pydantic import BaseModel, Field


class OfferIn(BaseModel):
    application_id: int
    principal: float = Field(gt=0, le=50000)
    term_months: int = Field(default=48, ge=12, le=60)
    annual_rate: float = Field(default=7.99, gt=0, le=35)


class ScheduleRow(BaseModel):
    n: int
    due_date: str
    payment: float
    principal: float
    interest: float
    balance: float


class Disclosure(BaseModel):
    apr: float
    finance_charge: float
    monthly_payment: float
    amount_financed: float
    total_of_payments: float
    schedule: list[ScheduleRow] = []


class OfferOut(BaseModel):
    app_id: int
    disclosure: Disclosure


class OfferResponse(BaseModel):
    offer_id: int
    application_id: int
    apr: float
    finance_charge: float
    monthly_payment: float
    total_of_payments: float
    disclosure: Disclosure
    schedule: list[ScheduleRow] = []


class DisclosureIn(BaseModel):
    """Inputs, never outputs. The service derives every disclosed figure itself, so no
    caller — agent or otherwise — can supply an APR or a finance charge."""

    offer_id: int
    decision_event_id: int
    principal: float = Field(gt=0, le=50000)
    term_months: int = Field(default=48, ge=12, le=60)
    annual_rate: float = Field(default=7.99, gt=0, le=35)


class DisclosureOut(BaseModel):
    """Money as integer minor units and the APR as a decimal STRING.

    Deliberately not float: this is the authoritative record, and a JSON float would
    reintroduce at the API boundary exactly the representation problem the minor-unit
    columns exist to avoid.
    """

    disclosure_id: int
    offer_id: int
    decision_event_id: int
    status: str
    apr: str
    finance_charge_cents: int
    amount_financed_cents: int
    monthly_payment_cents: int
    total_of_payments_cents: int
    fee_schedule_version: str
    apr_method_version: str
    content_fingerprint: str
    delivered_at: str | None = None


class ProvenanceOut(BaseModel):
    """One walk of the FK-as-graph chain: disclosure -> offer -> decision_event ->
    application -> applicant (ADR 0012, spec D3).

    `chain_complete` is derived here rather than by each caller: the pipeline gates on it
    and the UI badges on it, and two implementations of "is this chain whole" would drift.
    A partial chain is a legitimate result (legacy offers predate the provenance edge), so
    it is reported, not raised.
    """

    disclosure_id: int | None = None
    disclosure_status: str | None = None
    disclosed_apr: str | None = None
    compute_snapshot: dict | None = None
    fee_schedule_version: str | None = None
    apr_method_version: str | None = None
    content_fingerprint: str | None = None
    delivered_at: str | None = None
    offer_id: int | None = None
    offer_apr: float | None = None
    offer_created_at: str | None = None
    decision_event_id: int | None = None
    decision_outcome: str | None = None
    policy_band: str | None = None
    decided_at: str | None = None
    application_id: int | None = None
    applicant_id: int | None = None
    chain_complete: bool
    missing_edges: list[str] = []
