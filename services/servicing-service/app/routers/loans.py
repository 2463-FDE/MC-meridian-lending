"""Loan portfolio read API: list, detail, amortization schedule, payment history."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schedule
from ..database import get_session
from ..schemas import (
    LoanDetail,
    LoanListItem,
    Page,
    PaymentItem,
    PaymentsOut,
    ScheduleOut,
    ScheduleRow,
)

router = APIRouter(prefix="/loans", tags=["loans"])


def _mask_pan(pan: str | None) -> str | None:
    if not pan:
        return None
    return "•••• " + pan[-4:]


@router.get("", response_model=Page[LoanListItem])
def list_loans(
    session: Session = Depends(get_session),
    status: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    stmt = select(models.Loan, models.Balance).join(
        models.Balance, models.Balance.loan_id == models.Loan.id, isouter=True
    )
    count_stmt = select(func.count(models.Loan.id))
    if status and status != "all":
        stmt = stmt.where(models.Loan.status == status)
        count_stmt = count_stmt.where(models.Loan.status == status)
    total = session.scalar(count_stmt) or 0
    stmt = stmt.order_by(models.Loan.id).limit(limit).offset(offset)
    items = [
        LoanListItem(
            id=loan.id, applicant_name=loan.applicant_name, principal=loan.principal,
            apr=loan.apr, term_months=loan.term_months, status=loan.status,
            balance=(bal.balance if bal else 0.0), past_due=(bal.past_due if bal else 0.0),
            opened_at=loan.opened_at.isoformat() if loan.opened_at else None,
        )
        for loan, bal in session.execute(stmt).all()
    ]
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/{loan_id}", response_model=LoanDetail)
def get_loan(loan_id: int, session: Session = Depends(get_session)):
    loan = session.get(models.Loan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="loan not found")
    bal = session.get(models.Balance, loan_id)
    return LoanDetail(
        id=loan.id, applicant_name=loan.applicant_name, principal=loan.principal,
        apr=loan.apr, term_months=loan.term_months, status=loan.status,
        balance=(bal.balance if bal else 0.0), past_due=(bal.past_due if bal else 0.0),
        opened_at=loan.opened_at.isoformat() if loan.opened_at else None,
    )


@router.get("/{loan_id}/schedule", response_model=ScheduleOut)
def loan_schedule(loan_id: int, session: Session = Depends(get_session)):
    loan = session.get(models.Loan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="loan not found")
    rows = schedule.amortization(loan.principal, loan.apr, loan.term_months)
    return ScheduleOut(loan_id=loan_id, schedule=[ScheduleRow(**r) for r in rows])


@router.get("/{loan_id}/payments", response_model=PaymentsOut)
def loan_payments(loan_id: int, session: Session = Depends(get_session)):
    rows = session.scalars(
        select(models.Payment).where(models.Payment.loan_id == loan_id)
        .order_by(models.Payment.created_at.desc())
    ).all()
    items = [
        PaymentItem(
            id=p.id, amount=p.amount, method=p.method, masked_pan=_mask_pan(p.pan),
            created_at=p.created_at.isoformat() if p.created_at else None,
        )
        for p in rows
    ]
    return PaymentsOut(loan_id=loan_id, items=items)
