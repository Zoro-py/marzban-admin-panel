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
                # Baseline the monthly-average-usage window at whatever Marzban
                # already reports as this user's lifetime total — a reseller
                # onboarding an existing Marzban install has users with months of
                # real history that must NOT be attributed to "the last N days
                # since this dashboard started watching them."
                account.first_seen_traffic = mu.get("lifetime_used_traffic", 0)
                account.first_seen_traffic_at = now
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
