from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.auth import require_auth
from app.db import get_session
from app.models import Account, BillingMode, Customer, Group, LedgerEntry, LedgerSource, LedgerType, utcnow
from app.schemas import AccountRow, GroupCreate, GroupRead, GroupSettleRequest, GroupUpdate, GroupWithBalance
from app.services import billable_bytes, compute_balance, effective_rate, enrich_accounts

router = APIRouter(prefix="/api/groups", tags=["groups"], dependencies=[Depends(require_auth)])


def _invoice_lines(session: Session, accounts: list[Account], group: Group) -> list[dict]:
    """Each account's own rate wins, then the group's rate, then the
    dashboard-wide default (see services.effective_rate) — this is how a
    per-account discount (or markup) within a group works, and how a global
    default rate actually reaches group billing instead of only standalone
    accounts. Billable volume is the GROUP's mode for every member,
    regardless of that member's own billing_mode field (see
    services.billable_bytes / services.effective_billing_mode) — payg bills
    usage since the last settle, prepay bills each member's package
    (data_limit) itself."""
    lines = []
    for a in accounts:
        billable_gb = billable_bytes(a, group.billing_mode) / (1024**3)
        rate = effective_rate(session, a, group)
        lines.append(
            {
                "account_id": a.id,
                "marzban_username": a.marzban_username,
                "billable_gb": round(billable_gb, 3),
                "rate_per_gb": rate,
                "amount": round(billable_gb * rate, 2),
            }
        )
    return lines


def _with_balance(session: Session, g: Group) -> GroupWithBalance:
    charge, credit = compute_balance(session, group_id=g.id)
    accounts = session.exec(select(Account).where(Account.group_id == g.id)).all()
    lines = _invoice_lines(session, accounts, g)

    # last_settled_at/created_at round-trip through SQLite as naive even though
    # utcnow() produces an aware datetime (same quirk noted throughout this
    # codebase) — stay naive-UTC here too.
    now = utcnow().replace(tzinfo=None)
    cycle_start = g.last_settled_at or g.created_at
    next_due_at = cycle_start + timedelta(days=g.billing_cycle_days)

    return GroupWithBalance(
        **g.model_dump(),
        balance=charge - credit,
        account_count=len(accounts),
        # Real cumulative total (lifetime_used_traffic), NOT Marzban's own
        # used_traffic counter — that counter resets whenever Marzban applies a
        # data_limit reset for an account, independent of anything we track, so
        # summing it could (and did) come out LOWER than current_cycle_used_bytes
        # below, which is nonsensical for a figure labeled "lifetime": lifetime
        # must always be >= usage-since-our-last-settle by definition.
        total_used_traffic=sum(a.lifetime_used_traffic for a in accounts),
        current_cycle_used_bytes=sum(round(line["billable_gb"] * 1024**3) for line in lines),
        pending_amount=round(sum(line["amount"] for line in lines), 2),
        next_due_at=next_due_at,
        is_due=next_due_at <= now,
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


@router.get("/{group_id}/accounts", response_model=list[AccountRow])
def get_group_accounts(group_id: int, session: Session = Depends(get_session)):
    if not session.get(Group, group_id):
        raise HTTPException(404, "Group not found")
    accounts = session.exec(select(Account).where(Account.group_id == group_id)).all()
    return enrich_accounts(session, accounts)


@router.get("/{group_id}/invoice")
def get_group_invoice(group_id: int, session: Session = Depends(get_session)):
    """Usage-based invoice preview for the current, not-yet-settled cycle:
    each member account's usage since the group's last settlement (used_traffic
    minus that account's usage_baseline — Marzban's own "current usage" figure,
    matching what the operator sees in Marzban directly), times its effective
    rate. Purely a read — use POST /{group_id}/settle to actually charge it and
    roll the cycle forward."""
    group = session.get(Group, group_id)
    if not group:
        raise HTTPException(404, "Group not found")

    accounts = session.exec(select(Account).where(Account.group_id == group_id)).all()
    lines = _invoice_lines(session, accounts, group)
    return {
        "group_id": group_id,
        "rate_per_gb": group.rate_per_gb or 0,
        "cycle_started_at": group.last_settled_at,
        "lines": lines,
        "total_amount": round(sum(line["amount"] for line in lines), 2),
    }


@router.post("/{group_id}/settle")
def settle_group(group_id: int, body: GroupSettleRequest = GroupSettleRequest(), session: Session = Depends(get_session)):
    """Posts one `charge` ledger entry for the current cycle's total amount
    (usage-based for payg, package-based for prepay — see
    services.billable_bytes) against the group's representative customer,
    then rolls every member account's billing baseline forward so the next
    cycle starts from zero billable amount.

    This POSTS A CHARGE, not a payment record — pass mark_paid=True when the
    representative customer is paying in the same moment, to also post a
    matching credit so the balance nets back to 0 (settled) instead of
    showing as still owed."""
    group = session.get(Group, group_id)
    if not group:
        raise HTTPException(404, "Group not found")

    accounts = session.exec(select(Account).where(Account.group_id == group_id)).all()
    lines = _invoice_lines(session, accounts, group)
    total_amount = round(sum(line["amount"] for line in lines), 2)

    now = utcnow()
    cycle_note = (
        f"Package settlement for cycle ending {now.date().isoformat()}"
        if group.billing_mode == BillingMode.prepay
        else f"Usage settlement for cycle ending {now.date().isoformat()}"
    )
    if total_amount > 0:
        session.add(
            LedgerEntry(
                type=LedgerType.charge,
                amount=total_amount,
                customer_id=group.representative_customer_id,
                group_id=group.id,
                note=cycle_note,
                source=LedgerSource.web,
            )
        )
        if body.mark_paid:
            session.add(
                LedgerEntry(
                    type=LedgerType.credit,
                    amount=total_amount,
                    customer_id=group.representative_customer_id,
                    group_id=group.id,
                    note=f"Payment received at settlement ({now.date().isoformat()})",
                    source=LedgerSource.web,
                )
            )

    for a in accounts:
        if group.billing_mode == BillingMode.payg:
            a.usage_baseline = a.used_traffic
            a.usage_baseline_at = now
        else:
            a.billed_data_limit = a.data_limit or 0
        session.add(a)

    group.last_settled_at = now
    session.add(group)
    session.commit()

    return {"group_id": group_id, "charged_amount": total_amount, "settled_at": now, "lines": lines}


@router.post("/{group_id}/reset-cycle")
def reset_group_cycle(group_id: int, session: Session = Depends(get_session)):
    """Same as /settle EXCEPT it never posts a ledger charge — rolls every
    member's usage_baseline forward and starts a new cycle as if payment was
    already collected some other way (cash, a manual "New debt/credit" entry
    recorded separately, etc.). Without this, the only way to close out a
    cycle was to charge the computed pending amount, which double-bills a
    group whose members already paid outside the ledger."""
    group = session.get(Group, group_id)
    if not group:
        raise HTTPException(404, "Group not found")

    accounts = session.exec(select(Account).where(Account.group_id == group_id)).all()
    lines = _invoice_lines(session, accounts, group)

    now = utcnow()
    for a in accounts:
        if group.billing_mode == BillingMode.payg:
            a.usage_baseline = a.used_traffic
            a.usage_baseline_at = now
        else:
            a.billed_data_limit = a.data_limit or 0
        session.add(a)

    group.last_settled_at = now
    session.add(group)
    session.commit()

    return {"group_id": group_id, "reset_at": now, "lines": lines}
