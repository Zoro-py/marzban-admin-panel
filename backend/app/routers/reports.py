import time
from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from app.auth import require_auth
from app.db import get_session
from app.models import Account, Customer, Group, LedgerEntry, LedgerType, utcnow
from app.services import compute_balance, effective_rate, rate_is_configured

router = APIRouter(prefix="/api/reports", tags=["reports"], dependencies=[Depends(require_auth)])


@router.get("/summary")
def summary(
    quota_pct_threshold: float = 80.0,
    expiry_days_threshold: int = 3,
    session: Session = Depends(get_session),
):
    customers = session.exec(select(Customer)).all()
    overdue_customers = []
    for c in customers:
        charge, credit = compute_balance(session, customer_id=c.id)
        balance = charge - credit
        if balance > 0:
            overdue_customers.append({"customer_id": c.id, "name": c.name, "balance": balance})
    overdue_customers.sort(key=lambda x: -x["balance"])

    accounts = session.exec(select(Account)).all()
    groups = {g.id: g for g in session.exec(select(Group)).all()}
    now_ts = int(time.time())

    # Already-expired/exhausted accounts are a DIFFERENT problem than "about to"
    # ones (nothing to do but notice vs. still time to act) — kept as separate
    # buckets rather than lumped by threshold, per explicit product requirement.
    exhausted_accounts = []
    near_quota_accounts = []
    expired_accounts = []
    near_expiry_accounts = []
    no_rate_accounts = []

    for a in accounts:
        if a.data_limit:
            used_pct = round(a.used_traffic / a.data_limit * 100, 1)
            if used_pct >= 100:
                exhausted_accounts.append(
                    {"account_id": a.id, "marzban_username": a.marzban_username, "used_pct": used_pct}
                )
            elif used_pct >= quota_pct_threshold:
                near_quota_accounts.append(
                    {"account_id": a.id, "marzban_username": a.marzban_username, "used_pct": used_pct}
                )

        if a.expire:
            days_left = round((a.expire - now_ts) / 86400, 1)
            if days_left < 0:
                expired_accounts.append(
                    {"account_id": a.id, "marzban_username": a.marzban_username, "days_left": days_left}
                )
            elif days_left <= expiry_days_threshold:
                near_expiry_accounts.append(
                    {"account_id": a.id, "marzban_username": a.marzban_username, "days_left": days_left}
                )

        # "No group" is not itself a problem (every account is standalone unless
        # explicitly grouped) — what actually needs attention is an account where
        # NOTHING in its fallback chain (account -> group -> dashboard default)
        # was ever set. Checking configuredness rather than effective_rate <= 0
        # keeps an intentionally-comped account (rate explicitly set to 0) out
        # of this list — that's a deliberate choice, not a misconfiguration.
        if not rate_is_configured(session, a, groups.get(a.group_id)):
            no_rate_accounts.append({"account_id": a.id, "marzban_username": a.marzban_username})

    exhausted_accounts.sort(key=lambda x: -x["used_pct"])
    near_quota_accounts.sort(key=lambda x: -x["used_pct"])
    expired_accounts.sort(key=lambda x: x["days_left"])
    near_expiry_accounts.sort(key=lambda x: x["days_left"])

    return {
        "overdue_customers": overdue_customers,
        "exhausted_accounts": exhausted_accounts,
        "near_quota_accounts": near_quota_accounts,
        "expired_accounts": expired_accounts,
        "near_expiry_accounts": near_expiry_accounts,
        "no_rate_accounts": no_rate_accounts,
        "total_accounts": len(accounts),
        "total_customers": len(customers),
    }


@router.get("/finance")
def finance(session: Session = Depends(get_session)):
    """The financial overview the dashboard was missing entirely: total money
    outstanding/owed-back across everyone, this month's revenue vs. billed,
    a day-by-day revenue trend for the last 30 days, recent transactions, and
    every configured rate in one place."""
    customers = session.exec(select(Customer)).all()
    total_outstanding = 0.0
    total_credit_balance = 0.0
    for c in customers:
        charge, credit = compute_balance(session, customer_id=c.id)
        balance = charge - credit
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
    day = since.date()
    while day <= now.date():
        by_day[day.isoformat()] = 0.0
        day += timedelta(days=1)
    for e in all_entries:
        if e.type == LedgerType.credit and e.date >= since:
            by_day[e.date.date().isoformat()] += e.amount
    revenue_by_day = [{"date": d, "amount": round(amt, 2)} for d, amt in sorted(by_day.items())]

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
            "billing_mode": a.billing_mode,
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
        "recent_transactions": recent_transactions,
        "rate_overview": rate_overview,
    }
