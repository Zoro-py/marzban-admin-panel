from datetime import datetime

from sqlmodel import Session, select

from app.db import engine
from app.marzban_client import marzban_client
from app.models import Account, AccountEvent, LedgerSource, OnlineSnapshot, utcnow

PAGE_SIZE = 200

# An account counts as "currently online" if Marzban reported a connection
# within this many seconds of sync running. Marzban doesn't expose a live
# online/offline flag directly — only online_at, a last-seen timestamp — so
# this threshold is this dashboard's own definition, not Marzban's. 3 minutes
# comfortably covers normal client check-in intervals without counting
# someone who disconnected minutes ago as still online.
ONLINE_THRESHOLD_SECONDS = 180


def _parse_online_at(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        # Marzban returns ISO 8601; strip a trailing Z if present since SQLite/
        # our own datetimes are stored naive-UTC throughout this codebase.
        return datetime.fromisoformat(str(value).replace("Z", "")).replace(tzinfo=None)
    except ValueError:
        return None


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
        touched: list[Account] = []

        for mu in marzban_users:
            username = mu["username"]
            account = existing.get(username)
            if account is None:
                account = Account(marzban_username=username)
                # first_seen_traffic baselines the MONTHLY-AVERAGE-USAGE
                # ESTIMATE (a display figure) at this account's lifetime total
                # right now — averaging in months of pre-existing history
                # would produce a nonsensical inflated rate, since that usage
                # didn't happen "recently."
                lifetime = mu.get("lifetime_used_traffic", 0)
                account.first_seen_traffic = lifetime
                account.first_seen_traffic_at = now

                # usage_baseline is a DIFFERENT thing: what BILLING is measured
                # from. Deliberately left at the model default (0), NOT set to
                # `lifetime` — a reseller onboarding an existing Marzban
                # install has real, unpaid usage on day one, and the whole
                # point of this dashboard is to make that visible as debt, not
                # hide it because it predates the first sync that happened to
                # notice the account. (An earlier version of this code set it
                # to `lifetime` here on the "don't double-bill" theory that
                # pre-existing usage might already have been paid for outside
                # the system — in practice this made real debt disappear by
                # default, the opposite of what a billing system should do.)
                created += 1
            else:
                updated += 1

            # Captured before overwriting, for the external-change check below.
            # A change made THROUGH this dashboard's own Adjust endpoint is
            # never visible here: that endpoint already writes the new value
            # immediately, so by the time sync runs next, old == new and
            # nothing fires. Anything sync itself detects as a diff therefore
            # happened somewhere sync doesn't control — i.e. directly in
            # Marzban — which is exactly the "resilience to out-of-band
            # changes" gap: this doesn't bill for it (there's no agreed price
            # to infer), but it makes it visible in the account's History so
            # the operator can see it happened and decide whether to invoice.
            prev_data_limit = account.data_limit
            prev_expire = account.expire

            account.used_traffic = mu.get("used_traffic", 0)
            account.lifetime_used_traffic = mu.get("lifetime_used_traffic", 0)
            account.data_limit = mu.get("data_limit")
            account.expire = mu.get("expire")
            account.status = mu.get("status")
            account.online_at = _parse_online_at(mu.get("online_at"))
            account.last_synced_at = now
            session.add(account)
            touched.append(account)

            if account.id is not None:
                # Only compare when BOTH values are real numbers — None means
                # "unlimited"/"never expires" in Marzban's own semantics, not
                # zero, so a None-involved transition is a plan CHANGE (e.g.
                # limited -> unlimited), not a comparable "increase," and
                # treating None as 0 would produce a nonsense multi-year delta.
                if prev_data_limit is not None and account.data_limit is not None and prev_data_limit < account.data_limit:
                    added_gb = (account.data_limit - prev_data_limit) / (1024**3)
                    session.add(
                        AccountEvent(
                            account_id=account.id,
                            action="external_data_limit_increase",
                            detail=f"+{added_gb:.2f} GB added outside this dashboard (in Marzban directly)",
                            date=now,
                            source=LedgerSource.sync,
                        )
                    )
                if prev_expire is not None and account.expire is not None and prev_expire < account.expire:
                    added_days = (account.expire - prev_expire) / 86400
                    session.add(
                        AccountEvent(
                            account_id=account.id,
                            action="external_expire_extend",
                            detail=f"+{added_days:.1f} days added outside this dashboard (in Marzban directly)",
                            date=now,
                            source=LedgerSource.sync,
                        )
                    )

        # Recorded as a side effect of this sync, not a separate poller — a
        # dedicated online-count poller would mean extra Marzban logins/
        # requests on top of what sync already makes, working directly against
        # the "don't request more than necessary" goal. Trend granularity is
        # therefore exactly the sync interval. Counted from `touched` (every
        # account Marzban reported just now), not `existing` — that dict was
        # built before this loop and never gained the ones just created here.
        now_naive = now.replace(tzinfo=None)
        online_count = sum(
            1
            for a in touched
            if a.online_at is not None and (now_naive - a.online_at).total_seconds() <= ONLINE_THRESHOLD_SECONDS
        )
        session.add(OnlineSnapshot(recorded_at=now, online_count=online_count, total_accounts=len(touched)))

        session.commit()

    return {"marzban_user_count": len(marzban_users), "created": created, "updated": updated, "synced_at": now}
