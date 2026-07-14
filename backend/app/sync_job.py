from sqlmodel import Session, select

from app.db import engine
from app.marzban_client import marzban_client
from app.models import Account, utcnow

PAGE_SIZE = 200


async def _fetch_all_marzban_users() -> list[dict]:
    users: list[dict] = []
    offset = 0
    while True:
        page = await marzban_client.list_users(offset=offset, limit=PAGE_SIZE)
        batch = page.get("users", [])
        users.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return users


async def run_sync() -> dict:
    """Pulls every user from Marzban and mirrors usage/status/limits into the
    local Account table. Never touches ownership fields (customer_id/group_id) —
    those are only ever set by an operator action, never inferred from Marzban.
    A Marzban user with no local match yet is inserted unassigned so it shows
    up in the dashboard's "needs assignment" view."""
    marzban_users = await _fetch_all_marzban_users()
    now = utcnow()
    created = 0
    updated = 0

    with Session(engine) as session:
        existing = {a.marzban_username: a for a in session.exec(select(Account)).all()}

        for mu in marzban_users:
            username = mu["username"]
            account = existing.get(username)
            if account is None:
                account = Account(marzban_username=username)
                # Baseline BOTH the monthly-average-usage window AND the billing
                # cycle at whatever Marzban already reports as this user's
                # lifetime total. Without this, a reseller onboarding an existing
                # Marzban install with months of real pre-existing history would
                # have that entire history counted as "billable this cycle" on
                # the very first settle — usage_baseline defaults to 0, so
                # billable = lifetime_used_traffic - 0 = the account's ENTIRE
                # lifetime usage, not just usage since this dashboard started
                # tracking it. Both baselines start from the same point: the
                # moment this account first appeared here.
                lifetime = mu.get("lifetime_used_traffic", 0)
                account.first_seen_traffic = lifetime
                account.first_seen_traffic_at = now
                account.usage_baseline = lifetime
                account.usage_baseline_at = now
                created += 1
            else:
                updated += 1

            account.used_traffic = mu.get("used_traffic", 0)
            account.lifetime_used_traffic = mu.get("lifetime_used_traffic", 0)
            account.data_limit = mu.get("data_limit")
            account.expire = mu.get("expire")
            account.status = mu.get("status")
            account.last_synced_at = now
            session.add(account)

        session.commit()

    return {"marzban_user_count": len(marzban_users), "created": created, "updated": updated, "synced_at": now}
