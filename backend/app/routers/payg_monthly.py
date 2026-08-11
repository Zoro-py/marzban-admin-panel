from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.auth import require_auth
from app.db import get_session
from app.models import Account, Group, LedgerEntry, LedgerSource, LedgerType, MonthlySettlementBatch, utcnow
from app.payg_monthly_job import maybe_run_monthly_payg_settlement

router = APIRouter(prefix="/api/payg-monthly", tags=["payg-monthly"], dependencies=[Depends(require_auth)])


@router.post("/run")
async def trigger_monthly_settlement():
    """Runs the same check the daily schedule runs, immediately — lets an
    operator confirm the whole pipeline (Telegram gate, then settle+reset)
    actually works before trusting it to run unattended at month-end. Most
    days this is a no-op (nothing unsettled yet); see
    payg_monthly_job.maybe_run_monthly_payg_settlement for why."""
    try:
        return await maybe_run_monthly_payg_settlement()
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/periods")
def list_periods(session: Session = Depends(get_session)):
    """Every Jalali period ("1405-05") that has at least one settlement row,
    most recent first — populates the period picker on the Monthly
    Settlements page."""
    rows = session.exec(
        select(MonthlySettlementBatch.jalali_period).distinct().order_by(MonthlySettlementBatch.jalali_period.desc())
    ).all()
    return rows


@router.get("/batches")
def list_batches(period: str | None = None, session: Session = Depends(get_session)):
    """Settlement rows for one period — defaults to the most recent period
    that has any, so the page has something to show with zero params."""
    if period is None:
        period = session.exec(
            select(MonthlySettlementBatch.jalali_period).order_by(MonthlySettlementBatch.jalali_period.desc())
        ).first()
        if period is None:
            return {"period": None, "rows": []}
    rows = session.exec(
        select(MonthlySettlementBatch)
        .where(MonthlySettlementBatch.jalali_period == period)
        .order_by(MonthlySettlementBatch.settled_at.desc())
    ).all()
    return {"period": period, "rows": rows}


@router.post("/batches/{batch_id}/mark-paid")
def mark_batch_paid(batch_id: int, session: Session = Depends(get_session)):
    """Posts a credit for exactly this settlement's amount — not the
    entity's whole current balance, which could include unrelated debt from
    something else entirely. Lets the operator record "this month's bill got
    paid" the moment it actually happens, days after the charge was posted
    at settle time, without that gap affecting anything (see
    payg_monthly_job's own docstring — the charge already exists as a real
    ledger entry regardless of when this gets clicked)."""
    batch = session.get(MonthlySettlementBatch, batch_id)
    if not batch:
        raise HTTPException(404, "Settlement not found")
    if batch.marked_paid_at is not None:
        raise HTTPException(400, "Already marked paid")

    if batch.group_id is not None:
        group = session.get(Group, batch.group_id)
        if not group:
            raise HTTPException(404, "Group for this settlement no longer exists")
        customer_id = group.representative_customer_id
    else:
        # A standalone account's own settle_account credits customer_id +
        # account_id — mirrored here so this credit shows up the same way
        # the account's own "Record payment" would have.
        account = session.get(Account, batch.account_id)
        if not account:
            raise HTTPException(404, "Account for this settlement no longer exists")
        customer_id = account.customer_id

    now = utcnow()
    try:
        session.add(LedgerEntry(
            type=LedgerType.credit,
            amount=batch.amount,
            customer_id=customer_id,
            group_id=batch.group_id,
            account_id=batch.account_id,
            note=f"Monthly settlement paid — {batch.jalali_period}",
            source=LedgerSource.web,
        ))
        batch.marked_paid_at = now
        session.add(batch)
        session.commit()
        session.refresh(batch)
    except Exception:
        session.rollback()
        raise
    return batch
