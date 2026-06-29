"""SQLAlchemy ORM model for the payments table.

Money columns map to Float (DOUBLE PRECISION in Postgres — the float-money debt). The
`payments` table carries the full PAN + CVV (PCI debt) and has no idempotency key.
"""
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loans.id"), nullable=True)
    pan: Mapped[str | None] = mapped_column(String, nullable=True)   # full PAN stored (debt)
    cvv: Mapped[str | None] = mapped_column(String, nullable=True)   # CVV stored (debt)
    amount: Mapped[float] = mapped_column(Float)                     # money as float (debt)
    method: Mapped[str | None] = mapped_column(String, default="card")
    created_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
