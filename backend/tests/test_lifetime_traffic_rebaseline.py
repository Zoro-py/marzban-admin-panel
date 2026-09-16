"""Checks for the monthly-average-usage self-heal in sync_job.py: if
lifetime_used_traffic ever drops (a reset in Marzban directly, an
auto-activated next plan, or anything else — see the fix's own comment in
sync_job.py for why this can't be fully ruled out), first_seen_traffic must
re-baseline instead of permanently clamping the estimate to ~0.

Bug report (2026-09-16): a heavily, repeatedly auto-renewed account (5GB
every ~2 days) showed "1.8 GB/mo" — an account actually burning ~75GB/mo
read as if it barely used anything, because first_seen_traffic was frozen
from long ago while lifetime_used_traffic had been reset out from under it
at some point, silently and permanently.

Plain `python -m tests.test_lifetime_traffic_rebaseline` from `backend/`.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="lifetime_rebaseline_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""
os.environ["SHOP_BOT_API_KEY"] = ""
os.environ["DELEGATE_BOT_API_KEY"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select  # noqa: E402

from app import marzban_client as marzban_module  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.models import Account, AccountEvent, Customer, utcnow  # noqa: E402
from app.services import monthly_avg_usage  # noqa: E402
from app.sync_job import run_sync  # noqa: E402

init_db()

GB = 1024 ** 3
failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


class FakeMarzban:
    def __init__(self):
        self.users: dict[str, dict] = {}

    async def list_all_users(self, page_size: int = 200) -> list[dict]:
        return list(self.users.values())


fake = FakeMarzban()
marzban_module.marzban_client.list_all_users = fake.list_all_users

# Long-ago baseline: this account was first seen 100 days ago, having
# already accumulated 500GB of lifetime traffic by then (a long, unrelated
# history this dashboard correctly doesn't want to count).
long_ago = utcnow().replace(tzinfo=None) - timedelta(days=100)
with Session(engine) as session:
    customer = Customer(name="Matinipanah-like Customer")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    account = Account(
        marzban_username="matinipanah-test",
        customer_id=customer.id,
        used_traffic=0,
        lifetime_used_traffic=500 * GB,
        first_seen_traffic=500 * GB,
        first_seen_traffic_at=long_ago,
        data_limit=5 * GB,
        created_at=long_ago,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    account_id = account.id

# ---- sync cycle 1: lifetime_used_traffic comes back LOWER than stored
# (500GB -> 3GB) — simulating a reset that happened somewhere, whatever the
# cause. Before the fix, first_seen_traffic (500GB) would stay frozen
# forever, permanently clamping observed_bytes to 0. ----
fake.users["matinipanah-test"] = {
    "username": "matinipanah-test",
    "used_traffic": int(3 * GB),
    "lifetime_used_traffic": int(3 * GB),
    "data_limit": int(5 * GB),
    "expire": None,
    "status": "active",
    "subscription_url": "/sub/tok",
}
asyncio.run(run_sync())

with Session(engine) as session:
    acc = session.get(Account, account_id)
    check("first_seen_traffic re-baselined to the new (lower) lifetime value",
          acc.first_seen_traffic == int(3 * GB))
    check("first_seen_traffic_at moved forward to (approximately) now",
          (utcnow().replace(tzinfo=None) - acc.first_seen_traffic_at).total_seconds() < 60)
    events = session.exec(select(AccountEvent).where(
        AccountEvent.account_id == account_id, AccountEvent.action == "lifetime_traffic_counter_dropped"
    )).all()
    check("an AccountEvent was logged for the drop", len(events) == 1)

# ---- sync cycle 2, a bit later: real, continued heavy usage since the
# re-baseline (3GB -> 8GB, i.e. +5GB) should now show up in the average
# instead of being swallowed by the old frozen baseline. Backdated 5 days
# (not the reported 2) only because MIN_USAGE_SAMPLE_DAYS=3 requires at
# least that much observed history before monthly_avg_usage returns a
# number at all — the bug itself doesn't care about the exact window. ----
with Session(engine) as session:
    acc = session.get(Account, account_id)
    acc.first_seen_traffic_at = utcnow().replace(tzinfo=None) - timedelta(days=5)
    session.add(acc)
    session.commit()

fake.users["matinipanah-test"]["used_traffic"] = int(8 * GB)
fake.users["matinipanah-test"]["lifetime_used_traffic"] = int(8 * GB)
asyncio.run(run_sync())

with Session(engine) as session:
    acc = session.get(Account, account_id)
    avg_gb, confidence, observed_days = monthly_avg_usage(acc, utcnow().replace(tzinfo=None))
    # +5GB over ~5 days -> ~30 GB/month, NOT the near-zero a permanently
    # clamped baseline (the reported bug) would have shown.
    check(f"monthly average now reflects real recent usage (got {avg_gb} GB/mo, expected ~30)",
          avg_gb is not None and 24 <= avg_gb <= 36)

# ---- a normal sync with NO drop must NOT touch first_seen_traffic or log
# a spurious event. ----
with Session(engine) as session:
    acc = session.get(Account, account_id)
    before_first_seen = acc.first_seen_traffic
    before_first_seen_at = acc.first_seen_traffic_at
fake.users["matinipanah-test"]["used_traffic"] = int(9 * GB)
fake.users["matinipanah-test"]["lifetime_used_traffic"] = int(9 * GB)  # grew, did not drop
asyncio.run(run_sync())
with Session(engine) as session:
    acc = session.get(Account, account_id)
    check("no drop -> first_seen_traffic untouched", acc.first_seen_traffic == before_first_seen)
    check("no drop -> first_seen_traffic_at untouched", acc.first_seen_traffic_at == before_first_seen_at)
    events = session.exec(select(AccountEvent).where(
        AccountEvent.account_id == account_id, AccountEvent.action == "lifetime_traffic_counter_dropped"
    )).all()
    check("still exactly one drop event on record (no spurious second one)", len(events) == 1)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All lifetime-traffic re-baseline cases passed.")
