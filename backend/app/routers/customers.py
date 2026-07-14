from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlmodel import Session, select

from app.auth import require_auth
from app.db import get_session
from app.models import Account, Customer, Group
from app.schemas import AccountRow, CustomerCreate, CustomerRead, CustomerUpdate, CustomerWithBalance
from app.services import compute_balance, enrich_accounts

router = APIRouter(prefix="/api/customers", tags=["customers"], dependencies=[Depends(require_auth)])


def _represented_groups(session: Session) -> dict[int, list[str]]:
    by_rep: dict[int, list[str]] = defaultdict(list)
    for g in session.exec(select(Group)).all():
        by_rep[g.representative_customer_id].append(g.name)
    return by_rep


@router.get("", response_model=list[CustomerWithBalance])
def list_customers(session: Session = Depends(get_session)):
    customers = session.exec(select(Customer)).all()
    rep_groups = _represented_groups(session)
    result = []
    for c in customers:
        charge, credit = compute_balance(session, customer_id=c.id)
        account_count = session.exec(
            select(func.count()).select_from(Account).where(Account.customer_id == c.id)
        ).one()
        result.append(
            CustomerWithBalance(
                **c.model_dump(),
                balance=charge - credit,
                account_count=account_count,
                represented_group_names=rep_groups.get(c.id, []),
            )
        )
    return result


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
    charge, credit = compute_balance(session, customer_id=customer_id)
    account_count = session.exec(
        select(func.count()).select_from(Account).where(Account.customer_id == customer_id)
    ).one()
    rep_groups = _represented_groups(session)
    return CustomerWithBalance(
        **customer.model_dump(),
        balance=charge - credit,
        account_count=account_count,
        represented_group_names=rep_groups.get(customer_id, []),
    )


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
