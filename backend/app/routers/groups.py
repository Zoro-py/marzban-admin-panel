from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.auth import require_auth
from app.db import get_session
from app.models import Account, BillingMode, Customer, Group, LedgerEntry, LedgerSource, LedgerType, utcnow
from app.schemas import AccountRow, GroupCreate, GroupRead, GroupSettleRequest, GroupUpdate, GroupWithBalance
from app.services import (
    MoneyBook,
    account_posted_balance,
    billable_bytes,
    effective_rate,
    enrich_accounts,
    group_only_posted_balance,
)

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


def _with_balance(session: Session, g: Group, book: Optional[MoneyBook] = None) -> GroupWithBalance:
    book = book or MoneyBook(session)
    accounts = book.group_members(g)
    lines = _invoice_lines(session, accounts, g)

    # last_settled_at/created_at round-trip through SQLite as naive even though
    # utcnow() produces an aware datetime (same quirk noted throughout this
    # codebase) — stay naive-UTC here too.
    now = utcnow().replace(tzinfo=None)
    cycle_start = g.last_settled_at or g.created_at
    next_due_at = cycle_start + timedelta(days=g.billing_cycle_days)

    return GroupWithBalance(
        **g.model_dump(),
        # Every figure below is a roll-up of this group's members (see
        # MoneyBook), so the header can never disagree with the member rows
        # printed underneath it — which it did when the group re-queried its
        # own total independently: a member's payment landed in the group's
        # pool while the matching charge sat elsewhere, and the group showed
        # itself as a creditor while every member still owed money.
        balance=book.group_posted(g),
        net_owed=book.group_net(g),
        account_count=len(accounts),
        # Real cumulative total (lifetime_used_traffic), NOT Marzban's own
        # used_traffic counter — that counter resets whenever Marzban applies a
        # data_limit reset for an account, independent of anything we track, so
        # summing it could (and did) come out LOWER than current_cycle_used_bytes
        # below, which is nonsensical for a figure labeled "lifetime": lifetime
        # must always be >= usage-since-our-last-settle by definition.
        total_used_traffic=sum(a.lifetime_used_traffic for a in accounts),
        current_cycle_used_bytes=sum(round(line["billable_gb"] * 1024**3) for line in lines),
        pending_amount=book.group_pending(g),
        next_due_at=next_due_at,
        is_due=next_due_at <= now,
    )


@router.get("", response_model=list[GroupWithBalance])
def list_groups(session: Session = Depends(get_session)):
    book = MoneyBook(session)
    groups = session.exec(select(Group)).all()
    return [_with_balance(session, g, book) for g in groups]


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
    """Charges the current cycle's total amount (usage-based for payg,
    package-based for prepay — see services.billable_bytes) against the
    group's representative customer, then rolls every member account's
    billing baseline forward so the next cycle starts from zero billable
    amount.

    Posts ONE charge PER MEMBER (each attributed to that member's own
    account_id via its line in _invoice_lines), not a single pooled entry
    for the whole group — so a payment recorded later against one specific
    member (Record an invoice on that account) nets against THAT member's
    own share, not the group's shared total. The group's own aggregate
    balance is unaffected either way: it's still the sum of every entry
    carrying its group_id, attributed or not.

    This POSTS A CHARGE, not a payment record — pass mark_paid=True when the
    representative customer is paying in the same moment, to also post a
    matching credit per member so each member's balance nets back to 0
    (settled) instead of showing as still owed."""
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
    # Read every prior balance BEFORE adding any entry below: these issue
    # SELECTs, which autoflush pending adds, so reading inside the loop would
    # start folding in charges just posted.
    prior_balances: dict[int, float] = {}
    prior_group_only = 0.0
    if body.mark_paid:
        for line in lines:
            prior_balances[line["account_id"]] = account_posted_balance(session, line["account_id"])
        prior_group_only = group_only_posted_balance(session, group.id)

    paid_note = f"Payment received at settlement ({now.date().isoformat()})"
    for line in lines:
        if line["amount"] > 0:
            session.add(
                LedgerEntry(
                    type=LedgerType.charge,
                    amount=line["amount"],
                    customer_id=group.representative_customer_id,
                    group_id=group.id,
                    account_id=line["account_id"],
                    note=cycle_note,
                    source=LedgerSource.web,
                )
            )
        if body.mark_paid:
            # Per member, credit whatever that member still OWES once this
            # charge lands — which is NOT the same as the charge itself:
            #  - a member who already paid individually must not be credited
            #    twice (their credit is attributed to their own account_id),
            #  - a member carrying unpaid debt from an earlier cycle must
            #    still be cleared even when this cycle adds nothing, or
            #    "payment received" would silently do nothing while the
            #    dialog's preview promised a settled balance.
            credit_amount = round(max(0.0, prior_balances.get(line["account_id"], 0.0) + line["amount"]), 2)
            if credit_amount > 0:
                session.add(
                    LedgerEntry(
                        type=LedgerType.credit,
                        amount=credit_amount,
                        customer_id=group.representative_customer_id,
                        group_id=group.id,
                        account_id=line["account_id"],
                        note=paid_note,
                        source=LedgerSource.web,
                    )
                )

    # Group-level debt with no member to attribute it to (a setup fee, an
    # adjustment) is part of what the representative owes, so paying in full
    # has to clear it too.
    if body.mark_paid and prior_group_only > 0:
        session.add(
            LedgerEntry(
                type=LedgerType.credit,
                amount=round(prior_group_only, 2),
                customer_id=group.representative_customer_id,
                group_id=group.id,
                note=paid_note,
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
