"""SQLAlchemy ORM models for the LSS tables.

Money columns map to Float (DOUBLE PRECISION in Postgres — the float-money debt). The
`balances` table is a single mutable balance column (no ledger). The `payments` table
carries the full PAN + CVV (PCI debt) and has no idempotency key.
"""
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Loan(Base):
    __tablename__ = "loans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applicant_name: Mapped[str | None] = mapped_column(String, nullable=True)
    principal: Mapped[float] = mapped_column(Float)          # money as float (debt)
    apr: Mapped[float] = mapped_column(Float)
    term_months: Mapped[int] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String, default="current")
    opened_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Balance(Base):
    __tablename__ = "balances"

    loan_id: Mapped[int] = mapped_column(ForeignKey("loans.id"), primary_key=True)
    balance: Mapped[float] = mapped_column(Float)            # single mutable float (debt)
    past_due: Mapped[float] = mapped_column(Float, default=0)
    updated_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loans.id"), nullable=True)
    pan: Mapped[str | None] = mapped_column(String, nullable=True)   # full PAN stored (debt)
    cvv: Mapped[str | None] = mapped_column(String, nullable=True)   # CVV stored (debt)
    amount: Mapped[float] = mapped_column(Float)                     # money as float (debt)
    method: Mapped[str | None] = mapped_column(String, default="card")
    created_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
