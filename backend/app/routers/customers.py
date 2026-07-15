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


def _represented_groups(session: Session) -> dict[int, list[Group]]:
    by_rep: dict[int, list[Group]] = defaultdict(list)
    for g in session.exec(select(Group)).all():
        by_rep[g.representative_customer_id].append(g)
    return by_rep


def _account_count(session: Session, customer_id: int, represented_groups: list[Group]) -> int:
    """Direct accounts (customer_id == this customer) PLUS every account
    belonging to a group this customer represents — a representative
    customer's whole reason for existing is to bill that group, so counting
    only direct ownership made a customer who clearly manages a full group
    show up as having "0 accounts", which reads as broken rather than as the
    deliberate "billed via the group, not directly" distinction it actually
    is (still shown separately, correctly, on the customer detail page)."""
    direct = session.exec(select(func.count()).select_from(Account).where(Account.customer_id == customer_id)).one()
    if not represented_groups:
        return direct
    group_ids = [g.id for g in represented_groups]
    via_groups = session.exec(
        select(func.count()).select_from(Account).where(Account.group_id.in_(group_ids))
    ).one()
    return direct + via_groups


@router.get("", response_model=list[CustomerWithBalance])
def list_customers(session: Session = Depends(get_session)):
    customers = session.exec(select(Customer)).all()
    rep_groups = _represented_groups(session)
    result = []
    for c in customers:
        charge, credit = compute_balance(session, customer_id=c.id)
        groups_for_c = rep_groups.get(c.id, [])
        result.append(
            CustomerWithBalance(
                **c.model_dump(),
                balance=charge - credit,
                account_count=_account_count(session, c.id, groups_for_c),
                represented_group_names=[g.name for g in groups_for_c],
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
    rep_groups = _represented_groups(session)
    groups_for_c = rep_groups.get(customer_id, [])
    return CustomerWithBalance(
        **customer.model_dump(),
        balance=charge - credit,
        account_count=_account_count(session, customer_id, groups_for_c),
        represented_group_names=[g.name for g in groups_for_c],
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
