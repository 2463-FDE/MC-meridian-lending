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

    The prose contract is mirrored here too, not only in the assembler
    (`origination-service` `prompts/disclosure_assemble.py` OUTPUT_SCHEMA): `heading`,
    `payment_terms` and `prepayment` carry NO digits (`^\\D*$`), with the same length caps.
    `heading` is on the same footing as the two prose fields — it is borrower-facing text
    outside the `figures` check, so a title like "Truth in Lending Disclosure 9.58%" would
    otherwise persist a stale number the figure gate never sees. That check runs
    in the caller, and this is the authoritative persistence boundary — `create_disclosure`
    only compares `figures` against the recomputed outputs, so without the constraint here
    any internal caller, coordinator change, or replay/backfill could persist prose that
    restates a stale or conflicting number while the figure gate still passes, then move the
    document to `delivered`. A digit-run in prose also defeats the redactor's PAN check on a
    borrower-facing field. Enforced at parse so both the fresh POST and the replay/backfill
    path (both read `document` off this model) are refused identically.
    """

    model_config = ConfigDict(extra="forbid")

    heading: str = Field(max_length=120, pattern=r"^\D*$")
    figures: DocumentFigures
    payment_terms: str = Field(max_length=600, pattern=r"^\D*$")
    prepayment: str = Field(max_length=300, pattern=r"^\D*$")


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
    # The disclosure record's own decision edge (disclosures.decision_event_id), exposed
    # next to the offer-derived `decision_event_id` above so a consumer can see a split-brain
    # audit trail. Equal to `decision_event_id` for any healthy chain; a disagreement makes
    # the chain incomplete (see missing_edges) and delivery is refused.
    disclosure_decision_event_id: int | None = None
    decision_outcome: str | None = None
    # The outcome of the decision the disclosure record itself cites, next to the offer-edge
    # `decision_outcome` above. A present, non-'approve' value makes the chain incomplete
    # (see missing_edges) and delivery is refused; the offer-edge copy is NULL on a legacy
    # no-edge offer, so this is the one that reveals an unapproved back-book chain.
    disclosure_decision_outcome: str | None = None
    policy_band: str | None = None
    decided_at: str | None = None
    application_id: int | None = None
    applicant_id: int | None = None
    chain_complete: bool
    missing_edges: list[str] = []
