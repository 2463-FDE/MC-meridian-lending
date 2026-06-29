"""SQLAlchemy ORM model for the kyc_checks table.

KycCheck is copied verbatim from the origination LOS models — the table is CIP only:
no sanctions_screened / ubo_identified / ongoing_monitoring columns (debt D11).
"""
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class KycCheck(Base):
    __tablename__ = "kyc_checks"
    # CIP only — no sanctions_screened / ubo_identified / ongoing_monitoring columns (debt).
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    applicant_id: Mapped[int | None] = mapped_column(ForeignKey("applicants.id"), nullable=True)
    name_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    dob_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    address_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ssn_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
