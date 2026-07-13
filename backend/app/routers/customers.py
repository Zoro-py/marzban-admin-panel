from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlmodel import Session, select

from app.auth import require_auth
from app.db import get_session
from app.models import Account, Customer
from app.schemas import CustomerCreate, CustomerRead, CustomerUpdate, CustomerWithBalance
from app.services import compute_balance

router = APIRouter(prefix="/api/customers", tags=["customers"], dependencies=[Depends(require_auth)])


@router.get("", response_model=list[CustomerWithBalance])
def list_customers(session: Session = Depends(get_session)):
    customers = session.exec(select(Customer)).all()
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
    return CustomerWithBalance(**customer.model_dump(), balance=charge - credit, account_count=account_count)


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


@router.get("/{customer_id}/accounts")
def get_customer_accounts(customer_id: int, session: Session = Depends(get_session)):
    if not session.get(Customer, customer_id):
        raise HTTPException(404, "Customer not found")
    return session.exec(select(Account).where(Account.customer_id == customer_id)).all()
