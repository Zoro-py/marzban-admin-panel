"""Locks the once-per-day dedupe on the auto-queue's insufficient-data
notification (live 2026-10-23: account 183, zz_irancell_plain, 0.66 GB left
with too-thin history — the branch queues NOTHING, so the qualifying
condition stayed true every 60-second sync cycle and the operator got the
same Telegram message once a minute until a human acted).

Reproduces: a near-quota prepay account younger than MIN_USAGE_SAMPLE_DAYS →
exactly ONE notification on the first sync, silence on the immediately
following cycles, and a fresh notification only after the marker episode is
older than 24h.

Run from backend/: venv/Scripts/python.exe tests/test_auto_queue_insufficient_dedupe.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="auto_queue_insufficient_test_")) / "test.db"
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
from app import sync_job  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.models import Account, AccountEvent, Customer, utcnow  # noqa: E402
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

sent: list[str] = []


async def fake_notify(text: str, reply_markup: dict | None = None) -> None:
    sent.append(text)


sync_job._notify_admin = fake_notify


def seed_thin_history_account(username: str) -> int:
    """Near quota (0.34 of 1 GB used) but observed for only 0.1 days —
    under MIN_USAGE_SAMPLE_DAYS (3.0), so monthly_avg_usage returns None and
    the branch under test fires. Auto-renew ON (default): the spam needs the
    auto-queue armed."""
    now_naive = utcnow().replace(tzinfo=None)
    expire = int(time.time() + 20 * 86400)
    with Session(engine) as session:
        customer = Customer(name=f"{username} Customer")
        session.add(customer)
        session.commit()
        session.refresh(customer)
        account = Account(
            marzban_username=username,
            auto_renew_enabled=True,
            customer_id=customer.id,
            status="active",
            used_traffic=int(0.34 * GB),
            lifetime_used_traffic=int(0.34 * GB),
            first_seen_traffic=0,
            first_seen_traffic_at=now_naive - timedelta(days=0.1),
            data_limit=int(1 * GB),
            expire=expire,
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        fake.users[username] = {
            "username": username,
            "used_traffic": int(0.34 * GB),
            "lifetime_used_traffic": int(0.34 * GB),
            "data_limit": int(1 * GB),
            "expire": expire,
            "status": "active",
            "subscription_url": "/sub/tok",
        }
        return account.id


aid = seed_thin_history_account("zz_thin_history_test")

asyncio.run(run_sync())
check("first sync sends exactly one insufficient-data notice",
      sum("میانگین مصرف نداریم" in t for t in sent) == 1)

asyncio.run(run_sync())
asyncio.run(run_sync())
check("two more syncs in the same episode send NOTHING more",
      sum("میانگین مصرف نداریم" in t for t in sent) == 1)

with Session(engine) as session:
    events = session.exec(
        select(AccountEvent).where(
            AccountEvent.account_id == aid,
            AccountEvent.action == "auto_queue_insufficient_data",
        )
    ).all()
check("exactly one marker AccountEvent recorded", len(events) == 1)

# Backdate the marker past 24h: a NEW episode may notify again.
with Session(engine) as session:
    ev = session.exec(
        select(AccountEvent).where(
            AccountEvent.account_id == aid,
            AccountEvent.action == "auto_queue_insufficient_data",
        )
    ).first()
    ev.date = utcnow() - timedelta(hours=25)
    session.add(ev)
    session.commit()

asyncio.run(run_sync())
check("a day-old episode notifies again (exactly one more)",
      sum("میانگین مصرف نداریم" in t for t in sent) == 2)

with Session(engine) as session:
    events = session.exec(
        select(AccountEvent).where(
            AccountEvent.account_id == aid,
            AccountEvent.action == "auto_queue_insufficient_data",
        )
    ).all()
check("second episode recorded its own marker", len(events) == 2)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("all auto-queue insufficient-data dedupe cases passed.")
