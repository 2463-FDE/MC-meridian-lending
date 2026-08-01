"""SQLAlchemy ORM model for the offers table.

Money columns are mapped to Float — they are DOUBLE PRECISION in Postgres (the float-money
debt). The disclosure-service reads/writes the same `offers` table the LOS does.
"""
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Offer(Base):
    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"), nullable=True)
    apr: Mapped[float | None] = mapped_column(Float, nullable=True)             # float APR (debt)
    finance_charge: Mapped[float | None] = mapped_column(Float, nullable=True)
    monthly_payment: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount_financed: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_of_payments: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ADR 0012 provenance edge. Nullable in the DDL for rows written before the migration;
    # the write path requires it (see routers/disclosures.py).
    decision_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("decision_events.id"), nullable=True
    )


class Disclosure(Base):
    """The authoritative disclosure record (ADR 0012).

    Money is integer MINOR UNITS and the APR is exact Numeric — contrast Offer above,
    whose float columns are a rounded convenience copy. Where the two disagree, this row
    is the one with TILA legal weight.
    """

    __tablename__ = "disclosures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"), nullable=False)
    decision_event_id: Mapped[int] = mapped_column(
        ForeignKey("decision_events.id"), nullable=False
    )
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
    created_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
