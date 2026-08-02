"""SQLAlchemy ORM models for the offers and disclosures tables.

Money columns on `Offer` are mapped to Float — they are DOUBLE PRECISION in Postgres (the
float-money debt). The disclosure-service reads/writes the same `offers` table the LOS does.

**Foreign keys are declared in the DDL, not here.** `applications` and `decision_events`
belong to other services and are not mapped in this metadata, so a `ForeignKey("...")`
pointing at them cannot resolve — and SQLAlchemy resolves every FK in the metadata when it
sorts tables for a flush, so one unmapped target turns any INSERT into a
NoReferencedTableError. That is a 500 on the disclosure write path, and no stub-session
test sees it because nothing flushes. Mapping the other services' tables here to satisfy
the resolver would duplicate their schema in this service to buy nothing: there are no
relationships, the ORM never emits DDL, and the database enforces the constraints.
"""

from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Float, Integer, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Offer(Base):
    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # FK to applications.id in the DDL; see the module docstring for why not here.
    app_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    apr: Mapped[float | None] = mapped_column(Float, nullable=True)  # float APR (debt)
    finance_charge: Mapped[float | None] = mapped_column(Float, nullable=True)
    monthly_payment: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount_financed: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_of_payments: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[str | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # ADR 0012 provenance edge. FK to decision_events.id in the DDL. Nullable there for
    # rows written before the migration; the write path requires it (routers/disclosures.py).
    decision_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Disclosure(Base):
    """The authoritative disclosure record (ADR 0012).

    Money is integer MINOR UNITS and the APR is exact Numeric — contrast Offer above,
    whose float columns are a rounded convenience copy. Where the two disagree, this row
    is the one with TILA legal weight.
    """

    __tablename__ = "disclosures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Both are NOT NULL foreign keys in the DDL (offers.id, decision_events.id).
    offer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    decision_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    apr: Mapped[Decimal] = mapped_column(Numeric(9, 3), nullable=False)
    finance_charge_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_financed_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    monthly_payment_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_of_payments_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    compute_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fee_schedule_version: Mapped[str] = mapped_column(Text, nullable=False)
    apr_method_version: Mapped[str] = mapped_column(Text, nullable=False)
    content_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    # NOT NULL DEFAULT now() in the DDL. The server default has to be declared here too:
    # without it the mapper sends created_at=NULL on INSERT, the database default never
    # fires, and the write fails the NOT NULL constraint. A stub session never sees this.
    created_at: Mapped[str | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    delivered_at: Mapped[str | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
