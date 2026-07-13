import time

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from app.auth import require_auth
from app.db import get_session
from app.models import Account, Customer
from app.services import compute_balance

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
    now_ts = int(time.time())

    near_quota_accounts = []
    near_expiry_accounts = []
    unassigned = []

    for a in accounts:
        if a.data_limit:
            used_pct = round(a.used_traffic / a.data_limit * 100, 1)
            if used_pct >= quota_pct_threshold:
                near_quota_accounts.append(
                    {"account_id": a.id, "marzban_username": a.marzban_username, "used_pct": used_pct}
                )

        if a.expire:
            days_left = round((a.expire - now_ts) / 86400, 1)
            if days_left <= expiry_days_threshold:
                near_expiry_accounts.append(
                    {"account_id": a.id, "marzban_username": a.marzban_username, "days_left": days_left}
                )

        if a.customer_id is None and a.group_id is None:
            unassigned.append({"account_id": a.id, "marzban_username": a.marzban_username})

    near_quota_accounts.sort(key=lambda x: -x["used_pct"])
    near_expiry_accounts.sort(key=lambda x: x["days_left"])

    return {
        "overdue_customers": overdue_customers,
        "near_quota_accounts": near_quota_accounts,
        "near_expiry_accounts": near_expiry_accounts,
        "unassigned_accounts": unassigned,
        "total_accounts": len(accounts),
        "total_customers": len(customers),
    }
