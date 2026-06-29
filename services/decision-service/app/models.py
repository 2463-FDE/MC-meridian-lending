"""SQLAlchemy ORM models for the decision service.

The `decisions` table is mapped exactly as it exists in the shared schema: outcome only,
no reason / no model drivers / no timestamp (the missing decision audit trail). Carried
over verbatim from origination when decisioning was split into its own service.
"""
from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Decision(Base):
    __tablename__ = "decisions"
    # OUTCOME ONLY. No reason, no drivers, no inputs, no model-run timestamp. (debt D4)
    app_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), primary_key=True)
    outcome: Mapped[str] = mapped_column(String)
