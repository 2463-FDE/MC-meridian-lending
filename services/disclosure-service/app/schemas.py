"""Pydantic request/response models for the disclosure API."""

from pydantic import BaseModel, ConfigDict, Field


class OfferIn(BaseModel):
    """Inputs for the offer the borrower is shown.

    `decision_event_id` is the decision that AUTHORIZES this offer, recorded on the row at
    creation because that is the only moment it is known without inference. Optional so an
    offer for an application that predates `decision_events` still persists; when it is
    absent the offer carries no provenance edge and `create_disclosure` closes one the old
    way (same-application only).
    """

    application_id: int
    principal: float = Field(gt=0, le=50000)
    term_months: int = Field(default=48, ge=12, le=60)
    annual_rate: float = Field(default=7.99, gt=0, le=35)
    decision_event_id: int | None = None


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
    """The persisted offer. `decision_event_id` is echoed back so the caller disclosing this
    offer sends the event that authorized IT, not whichever event is latest by then — after
    a re-decision those differ, and `uq_offers_app` means the offer is never regenerated."""

    offer_id: int
    application_id: int
    apr: float
    finance_charge: float
    monthly_payment: float
    total_of_payments: float
    disclosure: Disclosure
    schedule: list[ScheduleRow] = []
    decision_event_id: int | None = None


class DocumentFigures(BaseModel):
    """The five disclosed figures as the document spells them, as STRINGS.

    Strings for the same reason the assembler's output schema uses them: a JSON number
    invites normalising or re-rounding a regulated figure somewhere in the chain. These are
    not trusted — `create_disclosure` parses each one and refuses the document unless it
    equals the figure this service derived itself.
    """

    model_config = ConfigDict(extra="forbid")

    apr: str
    finance_charge: str
    amount_financed: str
    total_of_payments: str
    monthly_payment: str


class DisclosureDocument(BaseModel):
    """The borrower-facing document as assembled upstream (spec D4 stage 3).

    `extra="forbid"` mirrors the assembler's `additionalProperties: False`: an unexpected
    field means the two shapes have drifted, and storing it would put an unvalidated field
    inside a regulated record.
    """

    model_config = ConfigDict(extra="forbid")

    heading: str
    figures: DocumentFigures
    payment_terms: str
    prepayment: str


class DisclosureIn(BaseModel):
    """Inputs, never outputs. The service derives every disclosed figure itself, so no
    caller — agent or otherwise — can supply an APR or a finance charge.

    `document` is the one exception in shape but not in posture: it carries figures, and
    they are still not accepted. Every figure in it is compared against this service's own
    recomputation and the whole request is refused on any disagreement, so the document can
    only ever be stored alongside numbers it agrees with.
    """

    offer_id: int
    decision_event_id: int
    principal: float = Field(gt=0, le=50000)
    term_months: int = Field(default=48, ge=12, le=60)
    annual_rate: float = Field(default=7.99, gt=0, le=35)
    document: DisclosureDocument | None = None


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


class TransitionIn(BaseModel):
    """A compliance action on a disclosure (spec D6).

    `reason_code` is required on a reject and is what routes the rework — wording and
    formatting go back to the maker, wrong terms go back to decisioning. An unrouted
    reject would leave a regulated document in draft with nobody owning the next step.
    """

    to_status: str
    reason_code: str | None = None


class TransitionOut(DisclosureOut):
    """The disclosure after the transition, plus where a reject sends the work."""

    routed_to: str | None = None


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
