import time
from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from app.auth import require_auth
from app.db import get_session
from app.models import Account, BillingMode, Customer, Group, LedgerEntry, LedgerType, OnlineSnapshot, utcnow
from app.services import MoneyBook, effective_billing_mode, effective_rate, rate_is_configured

router = APIRouter(prefix="/api/reports", tags=["reports"], dependencies=[Depends(require_auth)])

# 1 day / 3 days / 1 week / 1 month, as literal option names rather than a
# free-form hours param — keeps the frontend's range picker and this endpoint
# in lockstep instead of the UI inventing values the backend doesn't expect.
ONLINE_HISTORY_RANGES = {"1d": 1, "3d": 3, "1w": 7, "1m": 30}


@router.get("/summary")
def summary(
    quota_pct_threshold: float = 80.0,
    expiry_days_threshold: int = 3,
    session: Session = Depends(get_session),
):
    book = MoneyBook(session)
    customers = session.exec(select(Customer)).all()
    # "In debt" means what they owe NOW (invoiced or not), same definition as
    # every other screen — see services.MoneyBook.
    overdue_customers = []
    for c in customers:
        balance = book.customer_net(c)
        if balance > 0:
            overdue_customers.append({"customer_id": c.id, "name": c.name, "balance": balance})
    overdue_customers.sort(key=lambda x: -x["balance"])

    accounts = session.exec(select(Account)).all()
    groups = {g.id: g for g in session.exec(select(Group)).all()}
    now_ts = int(time.time())

    # Owner shown next to each flagged account: its customer, or its group —
    # an attention list of bare usernames forces the operator to look each one
    # up before acting on it ("whose account is this?").
    customer_names = {c.id: c.name for c in customers}

    def owner_of(a: Account) -> str | None:
        if a.customer_id is not None:
            return customer_names.get(a.customer_id)
        if a.group_id is not None and a.group_id in groups:
            return groups[a.group_id].name
        return None

    # Already-expired/exhausted accounts are a DIFFERENT problem than "about to"
    # ones (nothing to do but notice vs. still time to act) — kept as separate
    # buckets rather than lumped by threshold, per explicit product requirement.
    exhausted_accounts = []
    near_quota_accounts = []
    expired_accounts = []
    near_expiry_accounts = []
    no_rate_accounts = []
    unassigned_accounts = []

    for a in accounts:
        if a.data_limit:
            used_pct = round(a.used_traffic / a.data_limit * 100, 1)
            if used_pct >= 100:
                exhausted_accounts.append(
                    {"account_id": a.id, "marzban_username": a.marzban_username, "used_pct": used_pct, "owner_name": owner_of(a)}
                )
            elif used_pct >= quota_pct_threshold:
                near_quota_accounts.append(
                    {"account_id": a.id, "marzban_username": a.marzban_username, "used_pct": used_pct, "owner_name": owner_of(a)}
                )

        if a.expire:
            days_left = round((a.expire - now_ts) / 86400, 1)
            if days_left < 0:
                expired_accounts.append(
                    {"account_id": a.id, "marzban_username": a.marzban_username, "days_left": days_left, "owner_name": owner_of(a)}
                )
            elif days_left <= expiry_days_threshold:
                near_expiry_accounts.append(
                    {"account_id": a.id, "marzban_username": a.marzban_username, "days_left": days_left, "owner_name": owner_of(a)}
                )

        # The sync job deliberately inserts Marzban users it discovers as
        # unassigned "so they show up in the dashboard's needs-assignment
        # view" (its own words) — but no such view ever existed on the
        # dashboard. This bucket is that view.
        if a.customer_id is None and a.group_id is None:
            unassigned_accounts.append({"account_id": a.id, "marzban_username": a.marzban_username})

        # "No group" is not itself a problem (every account is standalone unless
        # explicitly grouped) — what actually needs attention is an account where
        # NOTHING in its fallback chain (account -> group -> dashboard default)
        # was ever set. Checking configuredness rather than effective_rate <= 0
        # keeps an intentionally-comped account (rate explicitly set to 0) out
        # of this list — that's a deliberate choice, not a misconfiguration.
        if not rate_is_configured(session, a, groups.get(a.group_id)):
            no_rate_accounts.append({"account_id": a.id, "marzban_username": a.marzban_username, "owner_name": owner_of(a)})

    exhausted_accounts.sort(key=lambda x: -x["used_pct"])
    near_quota_accounts.sort(key=lambda x: -x["used_pct"])
    expired_accounts.sort(key=lambda x: x["days_left"])
    near_expiry_accounts.sort(key=lambda x: x["days_left"])

    # Real money owed, for EVERY group regardless of billing mode — NOT gated
    # by billing_cycle_days (that field only says WHEN a cycle nominally ends,
    # not whether there's already a real balance sitting unpaid before that
    # date). Two distinct figures, both shown when present:
    #   - pending_amount: usage accrued since the last settle, at each
    #     member's effective rate — an ESTIMATE, never posted to the ledger by
    #     itself (settling turns it into a real charge). Computed for every
    #     group regardless of billing_mode, matching the group detail page's
    #     own "Pending" stat (_with_balance/_invoice_lines in groups.py,
    #     which never gated on billing_mode either) — a prepay group still
    #     only OWES money once actually settled/charged, but seeing what its
    #     usage is currently worth beforehand is exactly the same kind of
    #     preview a payg group already got.
    #   - balance: ALREADY charged, not yet paid, for EITHER billing mode.
    # is_due (cycle elapsed) is a secondary signal per entry, not a gate.
    now_dt = utcnow().replace(tzinfo=None)

    pending_settlement = []
    for g in groups.values():
        # Roll-ups from the shared MoneyBook, so a group's line here matches
        # its own page and the member rows on it exactly.
        pending = book.group_pending(g)
        balance = book.group_posted(g)
        net = book.group_net(g)
        if pending <= 0 and net <= 0:
            continue
        cycle_start = g.last_settled_at or g.created_at
        next_due_at = cycle_start + timedelta(days=g.billing_cycle_days)
        is_due = g.billing_mode == BillingMode.payg and next_due_at <= now_dt
        pending_settlement.append(
            {
                "type": "group",
                "id": g.id,
                "name": g.name,
                "billing_mode": g.billing_mode,
                "pending_amount": pending,
                "balance": balance,
                # What they actually owe right now — see AccountRow.net_owed:
                # posted debt and not-yet-invoiced usage are the same debt at
                # two stages, so a payment already made has to count against
                # usage not yet billed.
                "net_owed": net,
                "is_due": is_due,
                "days_overdue": round((now_dt - next_due_at).total_seconds() / 86400, 1) if is_due else None,
            }
        )

    # Standalone (non-grouped) accounts, straight off the same MoneyBook the
    # Accounts table reads, so clicking through from here can't contradict
    # what the dashboard just said.
    for a in accounts:
        if a.group_id is not None:
            continue
        pending = book.account_pending(a)
        balance = round(book.account_posted(a), 2)
        net = book.account_net(a)
        # Nothing pending AND nothing owed -> not a queue item. A negative net
        # (they're in credit) isn't either: that's not something to chase.
        if pending <= 0 and net <= 0:
            continue
        pending_settlement.append(
            {
                "type": "account",
                "id": a.id,
                "name": a.marzban_username,
                "billing_mode": a.billing_mode,
                "pending_amount": pending,
                "balance": balance,
                "net_owed": net,
                "is_due": None,
                "days_overdue": None,
            }
        )

    pending_settlement.sort(key=lambda x: -x["net_owed"])
    total_pending = round(sum(x["pending_amount"] for x in pending_settlement), 2)

    return {
        "overdue_customers": overdue_customers,
        "exhausted_accounts": exhausted_accounts,
        "near_quota_accounts": near_quota_accounts,
        "expired_accounts": expired_accounts,
        "near_expiry_accounts": near_expiry_accounts,
        "no_rate_accounts": no_rate_accounts,
        "unassigned_accounts": unassigned_accounts,
        "pending_settlement": pending_settlement,
        "total_pending": total_pending,
        "total_accounts": len(accounts),
        "total_customers": len(customers),
    }


@router.get("/finance")
def finance(session: Session = Depends(get_session)):
    """The financial overview the dashboard was missing entirely: total money
    outstanding/owed-back across everyone, this month's revenue vs. billed,
    a day-by-day revenue trend for the last 30 days, recent transactions, and
    every configured rate in one place."""
    book = MoneyBook(session)
    customers = session.exec(select(Customer)).all()
    # Summed over CUSTOMERS specifically — that level's roll-up already
    # includes the accounts they own and the groups they represent, each
    # exactly once (see services.MoneyBook), so nothing is counted twice and
    # nothing owned by an unassigned account is silently dropped.
    total_outstanding = 0.0
    total_credit_balance = 0.0
    for c in customers:
        balance = book.customer_net(c)
        if balance > 0:
            total_outstanding += balance
        else:
            total_credit_balance += -balance

    # LedgerEntry.date loses its tzinfo round-tripping through SQLite (comes back
    # naive) even though utcnow() produces an aware datetime — verified directly
    # rather than assumed, since comparing aware vs. naive raises TypeError.
    # Everything below stays naive-UTC to match what's actually stored.
    now = utcnow().replace(tzinfo=None)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    all_entries = session.exec(select(LedgerEntry)).all()

    revenue_this_month = sum(e.amount for e in all_entries if e.type == LedgerType.credit and e.date >= month_start)
    charged_this_month = sum(e.amount for e in all_entries if e.type == LedgerType.charge and e.date >= month_start)

    since = now - timedelta(days=30)
    by_day: dict[str, float] = defaultdict(float)
    charged_by_day_map: dict[str, float] = defaultdict(float)
    day = since.date()
    while day <= now.date():
        by_day[day.isoformat()] = 0.0
        charged_by_day_map[day.isoformat()] = 0.0
        day += timedelta(days=1)
    for e in all_entries:
        if e.date >= since:
            if e.type == LedgerType.credit:
                by_day[e.date.date().isoformat()] += e.amount
            else:
                charged_by_day_map[e.date.date().isoformat()] += e.amount
    revenue_by_day = [{"date": d, "amount": round(amt, 2)} for d, amt in sorted(by_day.items())]
    # Billed alongside collected, per day — the chart pairs them so "charged a
    # lot this week but nothing came in yet" is visible at a glance instead of
    # being two disconnected month totals.
    charged_by_day = [{"date": d, "amount": round(amt, 2)} for d, amt in sorted(charged_by_day_map.items())]

    customer_names = {c.id: c.name for c in customers}
    groups = {g.id: g for g in session.exec(select(Group)).all()}
    group_names = {g.id: g.name for g in groups.values()}

    recent = session.exec(select(LedgerEntry).order_by(LedgerEntry.date.desc()).limit(30)).all()
    recent_transactions = [
        {
            "id": e.id,
            "type": e.type,
            "amount": e.amount,
            "date": e.date,
            "note": e.note,
            "customer_name": customer_names.get(e.customer_id) if e.customer_id else None,
            "group_name": group_names.get(e.group_id) if e.group_id else None,
        }
        for e in recent
    ]

    accounts = session.exec(select(Account)).all()
    rate_overview = [
        {
            "account_id": a.id,
            "marzban_username": a.marzban_username,
            "customer_name": customer_names.get(a.customer_id) if a.customer_id else None,
            "group_name": group_names.get(a.group_id) if a.group_id else None,
            "rate_per_gb": effective_rate(session, a, groups.get(a.group_id)),
            "rate_configured": rate_is_configured(session, a, groups.get(a.group_id)),
            # Effective, not raw: a payg group's member reads as 'prepay' on
            # its own field until someone explicitly flips it, which almost
            # never happens since group settle bills it correctly either way.
            "billing_mode": effective_billing_mode(session, a, groups.get(a.group_id)),
            "effective_rate_source": (
                "account"
                if a.rate_per_gb is not None
                else "group"
                if a.group_id and groups.get(a.group_id) and groups[a.group_id].rate_per_gb is not None
                else "default"
            ),
        }
        for a in accounts
    ]

    return {
        "total_outstanding": round(total_outstanding, 2),
        "total_credit_balance": round(total_credit_balance, 2),
        "revenue_this_month": round(revenue_this_month, 2),
        "charged_this_month": round(charged_this_month, 2),
        "revenue_by_day": revenue_by_day,
        "charged_by_day": charged_by_day,
        "recent_transactions": recent_transactions,
        "rate_overview": rate_overview,
    }


@router.get("/online-history")
def online_history(range: str = "1d", session: Session = Depends(get_session)):
    """Online-accounts-count trend. Points come from OnlineSnapshot, written as
    a side effect of the regular sync job — there's no separate poller, so the
    granularity between points is exactly sync_interval_minutes, not real-time.
    Marzban has no historical online-count endpoint of its own to source this
    from instead."""
    days = ONLINE_HISTORY_RANGES.get(range, 1)
    since = utcnow().replace(tzinfo=None) - timedelta(days=days)
    stmt = (
        select(OnlineSnapshot)
        .where(OnlineSnapshot.recorded_at >= since)
        .order_by(OnlineSnapshot.recorded_at.asc())
    )
    points = session.exec(stmt).all()
    return {
        "range": range if range in ONLINE_HISTORY_RANGES else "1d",
        "points": [
            {"recorded_at": p.recorded_at, "online_count": p.online_count, "total_accounts": p.total_accounts}
            for p in points
        ],
    }
