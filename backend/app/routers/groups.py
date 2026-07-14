from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlmodel import Session, select

from app.auth import require_auth
from app.db import get_session
from app.models import Account, Customer, Group, LedgerEntry, LedgerSource, LedgerType, utcnow
from app.schemas import GroupCreate, GroupRead, GroupUpdate, GroupWithBalance
from app.services import compute_balance

router = APIRouter(prefix="/api/groups", tags=["groups"], dependencies=[Depends(require_auth)])


def _with_balance(session: Session, g: Group) -> GroupWithBalance:
    charge, credit = compute_balance(session, group_id=g.id)
    accounts = session.exec(select(Account).where(Account.group_id == g.id)).all()
    return GroupWithBalance(
        **g.model_dump(),
        balance=charge - credit,
        account_count=len(accounts),
        total_used_traffic=sum(a.used_traffic for a in accounts),
    )


@router.get("", response_model=list[GroupWithBalance])
def list_groups(session: Session = Depends(get_session)):
    groups = session.exec(select(Group)).all()
    return [_with_balance(session, g) for g in groups]


@router.post("", response_model=GroupRead)
def create_group(body: GroupCreate, session: Session = Depends(get_session)):
    if not session.get(Customer, body.representative_customer_id):
        raise HTTPException(404, "representative_customer_id not found")
    group = Group(**body.model_dump())
    session.add(group)
    session.commit()
    session.refresh(group)
    return group


@router.get("/{group_id}", response_model=GroupWithBalance)
def get_group(group_id: int, session: Session = Depends(get_session)):
    group = session.get(Group, group_id)
    if not group:
        raise HTTPException(404, "Group not found")
    return _with_balance(session, group)


@router.patch("/{group_id}", response_model=GroupRead)
def update_group(group_id: int, body: GroupUpdate, session: Session = Depends(get_session)):
    group = session.get(Group, group_id)
    if not group:
        raise HTTPException(404, "Group not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(group, field, value)
    session.add(group)
    session.commit()
    session.refresh(group)
    return group


@router.get("/{group_id}/accounts")
def get_group_accounts(group_id: int, session: Session = Depends(get_session)):
    if not session.get(Group, group_id):
        raise HTTPException(404, "Group not found")
    return session.exec(select(Account).where(Account.group_id == group_id)).all()


def _invoice_lines(accounts: list[Account], group_rate: float) -> list[dict]:
    """Each account's own rate_per_gb wins over the group's rate when set —
    this is how a per-account discount (or markup) within a group works."""
    lines = []
    for a in accounts:
        billable_bytes = max(0, a.lifetime_used_traffic - a.usage_baseline)
        billable_gb = billable_bytes / (1024**3)
        effective_rate = a.rate_per_gb if a.rate_per_gb is not None else group_rate
        lines.append(
            {
                "account_id": a.id,
                "marzban_username": a.marzban_username,
                "billable_gb": round(billable_gb, 3),
                "rate_per_gb": effective_rate,
                "amount": round(billable_gb * effective_rate, 2),
            }
        )
    return lines


@router.get("/{group_id}/invoice")
def get_group_invoice(group_id: int, session: Session = Depends(get_session)):
    """Usage-based invoice preview for the current, not-yet-settled cycle:
    each member account's usage since the group's last settlement (lifetime_used_traffic
    minus that account's usage_baseline), times the group's rate_per_gb. Purely a read —
    use POST /{group_id}/settle to actually charge it and roll the cycle forward."""
    group = session.get(Group, group_id)
    if not group:
        raise HTTPException(404, "Group not found")

    accounts = session.exec(select(Account).where(Account.group_id == group_id)).all()
    lines = _invoice_lines(accounts, group.rate_per_gb or 0)
    return {
        "group_id": group_id,
        "rate_per_gb": group.rate_per_gb or 0,
        "cycle_started_at": group.last_settled_at,
        "lines": lines,
        "total_amount": round(sum(line["amount"] for line in lines), 2),
    }


@router.post("/{group_id}/settle")
def settle_group(group_id: int, session: Session = Depends(get_session)):
    """Posts one `charge` ledger entry for the current cycle's total usage-based
    amount against the group's representative customer, then rolls every member
    account's usage_baseline forward so the next cycle starts from zero billable usage."""
    group = session.get(Group, group_id)
    if not group:
        raise HTTPException(404, "Group not found")

    accounts = session.exec(select(Account).where(Account.group_id == group_id)).all()
    lines = _invoice_lines(accounts, group.rate_per_gb or 0)
    total_amount = round(sum(line["amount"] for line in lines), 2)

    now = utcnow()
    if total_amount > 0:
        session.add(
            LedgerEntry(
                type=LedgerType.charge,
                amount=total_amount,
                customer_id=group.representative_customer_id,
                group_id=group.id,
                note=f"Usage settlement for cycle ending {now.date().isoformat()}",
                source=LedgerSource.web,
            )
        )

    for a in accounts:
        a.usage_baseline = a.lifetime_used_traffic
        a.usage_baseline_at = now
        session.add(a)

    group.last_settled_at = now
    session.add(group)
    session.commit()

    return {"group_id": group_id, "charged_amount": total_amount, "settled_at": now, "lines": lines}
