"""SQLAlchemy ORM model for the offers table.

Money columns are mapped to Float — they are DOUBLE PRECISION in Postgres (the float-money
debt). The disclosure-service reads/writes the same `offers` table the LOS does.
"""
from sqlalchemy import DateTime, Float, ForeignKey, Integer
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
