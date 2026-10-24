"""The expired-flap regression (live incident 2026-10-24: Keyhan/Amir_taheri).

A group-payg member with a PAST expire: Marzban core marks it expired; the
2026-10-20 cap-hit retry used to match ANY non-active status, so sync PUT
status=active every 60s, Marzban re-expired it seconds later (expire
unchanged), notify fired each round — an infinite flap plus a Telegram spam
wave to the operator. The retry must run ONLY for 'limited' (Marzban's
data-limit block — the state it was written for).

Plain `python -m tests.test_sync_expired_flap` from `backend/`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="flap_test_")) / "test.db"
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

from sqlmodel import Session  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app.models import Account, BillingMode, Customer, Group  # noqa: E402
from app.sync_job import _maybe_settle_payg_cap_hit  # noqa: E402
from app import sync_job as sync_module  # noqa: E402

init_db()

failures: list[str] = []
calls: list[str] = []


async def _fake_modify(username: str, payload: dict) -> dict:
    calls.append(username)
    return {"username": username, "status": payload.get("status", "active")}


async def _no_notify(*a, **k):
    return True


async def _fake_reset(username: str) -> dict:
    calls.append("reset:" + username)
    return {"username": username, "used_traffic": 0, "status": "active"}


sync_module.marzban_client.modify_user = _fake_modify
sync_module.marzban_client.reset_user = _fake_reset
sync_module._notify_admin = _no_notify


def check(name, cond, extra=None):
    print(("  [OK] " if cond else "[FAIL] ") + name + (f"  {extra}" if (extra is not None and not cond) else ""))
    if not cond:
        failures.append(name)


import asyncio  # noqa: E402

now = datetime.utcnow().replace(tzinfo=None)

with Session(engine) as s:
    cust = Customer(name="Flap Customer")
    s.add(cust)
    s.flush()
    grp = Group(name="Flap Grp", representative_customer_id=cust.id, billing_mode=BillingMode.payg)
    s.add(grp)
    s.flush()
    # the LIVE shape of the incident: group-payg member, healthy meter (0 of
    # 5 GB used), expire already past, Marzban marking it expired
    expired_acc = Account(
        marzban_username="flap_expired", customer_id=cust.id, group_id=grp.id,
        billing_mode=BillingMode.prepay,  # own field irrelevant — group is payg
        data_limit=5 * 1024**3, used_traffic=0, status="expired",
        expire=int((now - timedelta(hours=1)).timestamp()),
    )
    limited_acc = Account(
        marzban_username="flap_limited", customer_id=cust.id, group_id=grp.id,
        billing_mode=BillingMode.prepay,
        data_limit=5 * 1024**3, used_traffic=0, status="limited",
        expire=int((now + timedelta(days=30)).timestamp()),
    )
    s.add(expired_acc)
    s.add(limited_acc)
    s.commit()
    CID, LID = expired_acc.id, limited_acc.id

# ── expired + healthy meter → the retry must do NOTHING ──
calls.clear()
with Session(engine) as s:
    acc = s.get(Account, CID)
    asyncio.run(_maybe_settle_payg_cap_hit(s, acc, now))
check("expired: NO modify_user (the flap is dead)", len(calls) == 0, calls)

# ── limited + healthy meter → the retry DOES run (what it was written for) ──
calls.clear()
with Session(engine) as s:
    acc = s.get(Account, LID)
    asyncio.run(_maybe_settle_payg_cap_hit(s, acc, now))
check("limited: retry reactivation runs", calls == ["flap_limited"], calls)

# ── expired + meter OVER the cap → the real cap-hit path still fires ──
calls.clear()
with Session(engine) as s:
    acc = s.get(Account, CID)
    acc.used_traffic = 5 * 1024**3  # at the cap
    s.add(acc)
    s.commit()
    # the cap-hit path notifies first; keep the fake notify in place
    asyncio.run(_maybe_settle_payg_cap_hit(s, acc, now))
check("expired at cap: cap-hit path still engaged (reset attempted)", any(c.startswith("reset:flap_expired") for c in calls), calls)
check("expired at cap: no bare status=active PUT", not any(c == "flap_expired" for c in calls), calls)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All expired-flap regression cases passed.")
