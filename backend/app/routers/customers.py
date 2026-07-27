from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.auth import require_auth
from app.db import get_session
from app.models import Account, Customer, Group
from app.schemas import AccountRow, CustomerCreate, CustomerRead, CustomerUpdate, CustomerWithBalance
from app.services import MoneyBook, enrich_accounts

router = APIRouter(prefix="/api/customers", tags=["customers"], dependencies=[Depends(require_auth)])


def _represented_groups(session: Session) -> dict[int, list[Group]]:
    by_rep: dict[int, list[Group]] = defaultdict(list)
    for g in session.exec(select(Group)).all():
        by_rep[g.representative_customer_id].append(g)
    return by_rep


def _account_count(book: MoneyBook, customer: Customer, represented_groups: list[Group]) -> int:
    """Accounts this customer pays for: the ones they own directly, plus every
    member of a group they represent — a representative customer's whole
    reason for existing is to bill that group, so counting only direct
    ownership made a customer who clearly manages a full group show up as
    having "0 accounts", which reads as broken rather than as the deliberate
    "billed via the group, not directly" distinction it actually is (still
    shown separately, correctly, on the customer detail page). Same split the
    balance roll-up uses, so the count and the money always agree."""
    direct = len(book.customer_accounts(customer))
    return direct + sum(len(book.group_members(g)) for g in represented_groups)


def _with_balance(book: MoneyBook, c: Customer, groups_for_c: list[Group]) -> CustomerWithBalance:
    return CustomerWithBalance(
        **c.model_dump(),
        # Roll-up of the accounts they own plus the groups they represent —
        # see services.MoneyBook. Reading this customer's own ledger rows
        # instead would both miss group money and double-count it, since a
        # group settle writes the representative's customer_id too.
        balance=book.customer_posted(c),
        net_owed=book.customer_net(c),
        account_count=_account_count(book, c, groups_for_c),
        represented_group_names=[g.name for g in groups_for_c],
    )


@router.get("", response_model=list[CustomerWithBalance])
def list_customers(
    offset: int = 0,
    limit: Optional[int] = None,
    session: Session = Depends(get_session)
):
    book = MoneyBook(session)
    stmt = select(Customer).offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    customers = session.exec(stmt).all()
    rep_groups = _represented_groups(session)
    return [_with_balance(book, c, rep_groups.get(c.id, [])) for c in customers]


@router.post("", response_model=CustomerRead)
def create_customer(body: CustomerCreate, session: Session = Depends(get_session)):
    customer = Customer(**body.model_dump())
    session.add(customer)
    session.commit()
    session.refresh(customer)
    return customer


@router.get("/{customer_id}", response_model=CustomerWithBalance)
def get_customer(customer_id: int, session: Session = Depends(get_session)):
    customer = session.get(Customer, customer_id)
    if not customer:
        raise HTTPException(404, "Customer not found")
    rep_groups = _represented_groups(session)
    return _with_balance(MoneyBook(session), customer, rep_groups.get(customer_id, []))


@router.patch("/{customer_id}", response_model=CustomerRead)
def update_customer(customer_id: int, body: CustomerUpdate, session: Session = Depends(get_session)):
    customer = session.get(Customer, customer_id)
    if not customer:
        raise HTTPException(404, "Customer not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(customer, field, value)
    session.add(customer)
    session.commit()
    session.refresh(customer)
    return customer


@router.get("/{customer_id}/accounts", response_model=list[AccountRow])
def get_customer_accounts(customer_id: int, session: Session = Depends(get_session)):
    if not session.get(Customer, customer_id):
        raise HTTPException(404, "Customer not found")
    accounts = session.exec(select(Account).where(Account.customer_id == customer_id)).all()
    return enrich_accounts(session, accounts)
