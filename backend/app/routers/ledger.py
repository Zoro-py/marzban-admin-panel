from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.auth import require_auth
from app.db import get_session
from app.models import Account, Customer, Group, LedgerEntry
from app.schemas import BalanceRead, LedgerCreate, LedgerRead
from app.services import MoneyBook

router = APIRouter(prefix="/api/ledger", tags=["ledger"], dependencies=[Depends(require_auth)])


@router.get("", response_model=list[LedgerRead])
def list_ledger(
    customer_id: Optional[int] = None,
    group_id: Optional[int] = None,
    account_id: Optional[int] = None,
    offset: int = 0,
    limit: int = 200,
    session: Session = Depends(get_session),
):
    stmt = select(LedgerEntry).order_by(LedgerEntry.date.desc()).offset(offset).limit(limit)
    if customer_id is not None:
        stmt = stmt.where(LedgerEntry.customer_id == customer_id)
    if group_id is not None:
        stmt = stmt.where(LedgerEntry.group_id == group_id)
    if account_id is not None:
        stmt = stmt.where(LedgerEntry.account_id == account_id)
    return session.exec(stmt).all()


@router.post("", response_model=LedgerRead)
def create_ledger_entry(body: LedgerCreate, session: Session = Depends(get_session)):
    if body.customer_id is None and body.group_id is None:
        raise HTTPException(400, "Provide customer_id and/or group_id for this ledger entry")
    if body.customer_id is not None and not session.get(Customer, body.customer_id):
        raise HTTPException(404, "customer_id not found")
    if body.group_id is not None and not session.get(Group, body.group_id):
        raise HTTPException(404, "group_id not found")
    if body.account_id is not None and not session.get(Account, body.account_id):
        raise HTTPException(404, "account_id not found")
    if body.amount <= 0:
        raise HTTPException(400, "amount must be positive; use `type` to indicate charge vs credit")

    entry = LedgerEntry(**body.model_dump())
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return entry


@router.get("/balance", response_model=BalanceRead)
def get_balance(
    customer_id: Optional[int] = None,
    group_id: Optional[int] = None,
    session: Session = Depends(get_session),
):
    if (customer_id is None) == (group_id is None):
        raise HTTPException(400, "Provide exactly one of customer_id or group_id")

    # Roll-ups, not a raw scan of rows carrying this id — see
    # services.MoneyBook for why those two are not the same thing.
    book = MoneyBook(session)
    if customer_id is not None:
        customer = session.get(Customer, customer_id)
        if not customer:
            raise HTTPException(404, "customer_id not found")
        balance = book.customer_posted(customer)
    else:
        group = session.get(Group, group_id)
        if not group:
            raise HTTPException(404, "group_id not found")
        balance = book.group_posted(group)

    # total_charge/total_credit are reported as the netted balance split into
    # its sign, rather than gross sums: a roll-up has no single meaningful
    # gross figure once it spans several accounts and groups.
    return BalanceRead(
        entity_type="customer" if customer_id is not None else "group",
        entity_id=customer_id if customer_id is not None else group_id,
        total_charge=max(0.0, balance),
        total_credit=max(0.0, -balance),
        balance=balance,
    )
