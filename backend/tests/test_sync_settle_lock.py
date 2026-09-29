"""The sync↔settle window (D8, checklist 2026-09-29 area 2.3) — the exact
red test mandated by docs/proposals/2026-09-28_sync_settle_lock_design.md.

Two writers touch ONE account inside one sync tick:
  writer A = a sync-job money site (`_maybe_settle_payg_cap_hit`, or
             `_activate_next_plan`);
  writer B = the API settle endpoint (POST /api/accounts/{id}/settle,
             which holds services.billing_lock via @serialise_billing).

A holds no billing lock today (it has its own _sync_lock, a different lock),
so B can interleave between A's read of the meter and A's commit+reset — the
same consumption gets charged twice (once by each writer). A gate planted in
the fake Marzban client makes the interleaving deterministic instead of
timing-luck: A suspends at its Marzban await exactly while B runs to
completion.

Expected AFTER the fix (both sites inside `async with billing_lock:`):
the writers serialise — the second reader sees the post-reset/committed
meter, so each GB of consumption is charged exactly once:
  scenario 1: ONE 10 GB charge (before: two 10 GB charges — 20,000 T invented);
  scenario 2: the ended plan's charge exactly once (before: two 10 GB charges
              covering the same ended package).

Plain `python -m tests.test_sync_settle_lock` from `backend/`.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="sync_settle_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""
os.environ["SHOP_BOT_API_KEY"] = ""
os.environ["DELEGATE_BOT_API_KEY"] = "test-delegate-key"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import marzban_client as marzban_module  # noqa: E402
import app.sync_job as sync_job  # noqa: E402
from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account, AccountRole, AppSettings, BillingMode, Customer, LedgerEntry,
    QueuedPlan, QueuedPlanStatus,
)

init_db()

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


GB = 1024 ** 3
RATE = 1000.0


class Gate:
    """A ONE-SHOT suspension point planted at a Marzban await: the FIRST
    caller (writer A, the sync job) parks here until the test says go, which
    guarantees writer B's whole read-compute-commit happens INSIDE A's
    read→commit window. Later callers (writer B's own Marzban calls — a payg
    settle resets the meter too) pass straight through, or the test would
    deadlock against itself."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.armed = True

    async def wait_in(self):
        if not self.armed:
            return
        self.armed = False
        self.entered.set()
        await self.release.wait()

    def __repr__(self):
        return f"<Gate entered={self.entered.is_set()} armed={self.armed}>"


class FakeMarzban:
    def __init__(self):
        self.panel: dict[str, dict] = {}
        self.reset_gate: Gate | None = None
        self.modify_gate: Gate | None = None

    async def modify_user(self, username: str, payload: dict) -> dict:
        if self.modify_gate is not None:
            await self.modify_gate.wait_in()
        user = self.panel.setdefault(username, {"username": username})
        user.update({k: v for k, v in payload.items() if k in ("data_limit", "expire", "status", "used_traffic")})
        return dict(user)

    async def reset_user(self, username: str) -> dict:
        if self.reset_gate is not None:
            await self.reset_gate.wait_in()
        user = self.panel.setdefault(username, {"username": username})
        user["used_traffic"] = 0
        if user.get("status") == "limited":
            user["status"] = "active"
        return dict(user)

    async def list_all_users(self) -> list[dict]:
        return []


fake = FakeMarzban()
# Bound-method assignment onto the real singleton — same idiom as
# test_delegate_concurrency.py; a module-attribute swap would silently miss
# every router that imported the object directly.
real_client = marzban_module.marzban_client
real_client.modify_user = fake.modify_user
real_client.reset_user = fake.reset_user
real_client.list_all_users = fake.list_all_users


async def _fake_notify(*a, **k) -> None:
    return None


# The sync job's admin notifications must never touch Telegram from a test.
sync_job._notify_admin = _fake_notify

app.dependency_overrides[require_auth] = lambda: "test-admin"

with Session(engine) as session:
    session.add(AppSettings(id=1, default_rate_per_gb=RATE))
    session.commit()


async def settle_via_api(account_id: int):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.post(f"/api/accounts/{account_id}/settle", json={})


def charges_for(account_id: int) -> list[LedgerEntry]:
    with Session(engine) as session:
        return session.exec(
            select(LedgerEntry).where(
                LedgerEntry.account_id == account_id, LedgerEntry.type == "charge"
            )
        ).all()


# ---------------------------------------------------------------- scenario 1
async def scenario_cap_hit_vs_settle() -> None:
    print("scenario 1: sync payg cap-hit vs API settle, one account")
    with Session(engine) as session:
        customer = Customer(name="D8 S1")
        session.add(customer)
        session.commit()
        session.refresh(customer)
        acc = Account(
            marzban_username="d8_cap", customer_id=customer.id,
            role=AccountRole.primary, billing_mode=BillingMode.payg,
            data_limit=10 * GB, used_traffic=10 * GB,  # cap hit exactly
            usage_baseline=0, status="limited",
        )
        session.add(acc)
        session.commit()
        session.refresh(acc)
        account_id = acc.id
        fake.panel["d8_cap"] = {"username": "d8_cap", "used_traffic": 10 * GB,
                                "status": "limited", "data_limit": 10 * GB}

    gate = Gate()
    fake.reset_gate = gate
    now = datetime.utcnow()

    async def writer_a():
        with Session(engine) as session:
            acc = session.get(Account, account_id)
            await sync_job._maybe_settle_payg_cap_hit(session, acc, now)

    task_a = asyncio.create_task(writer_a())
    await asyncio.wait_for(gate.entered.wait(), timeout=10)
    task_b = asyncio.create_task(settle_via_api(account_id))
    # Both worlds, one choreography: give B a short grace to finish. On the
    # UNFIXED code B completes inside A's parked window (the red interleaving
    # — recorded 2026-09-29: doubled 10 GB charges). On the FIXED code B is
    # blocked on the billing lock A holds, the grace expires, so A is
    # released first and B strictly follows — for ANY timing, because the
    # lock makes that ordering structural.
    done, _ = await asyncio.wait({task_b}, timeout=2.0)
    if task_b not in done:
        gate.release.set()
        await asyncio.wait_for(task_a, timeout=30)
    await asyncio.wait_for(task_b, timeout=30)
    gate.release.set()
    fake.reset_gate = None

    charges = charges_for(account_id)
    total = sum(c.amount for c in charges)
    ten_gb = [c for c in charges if c.gb_amount is not None and abs(c.gb_amount - 10.0) < 0.01]
    check("the capped 10 GB were charged exactly ONCE (was: twice)",
          len(ten_gb) == 1 and abs(total - 10 * RATE) < 0.01,
          f"charges={len(charges)}, 10GB-charges={len(ten_gb)}, total={total:g}")
    with Session(engine) as session:
        acc = session.get(Account, account_id)
        check("the meter was zeroed and the baseline rolled forward once",
              acc.used_traffic == 0 and acc.usage_baseline == 0,
              f"used={acc.used_traffic}, baseline={acc.usage_baseline}")


# ---------------------------------------------------------------- scenario 2
async def scenario_activation_vs_settle() -> None:
    print("scenario 2: next-plan activation vs API settle, one account")
    with Session(engine) as session:
        customer = Customer(name="D8 S2")
        session.add(customer)
        session.commit()
        session.refresh(customer)
        acc = Account(
            marzban_username="d8_act", customer_id=customer.id,
            role=AccountRole.primary, billing_mode=BillingMode.prepay,
            data_limit=10 * GB, used_traffic=10 * GB,
            billed_data_limit=0,  # the ended package was never invoiced
            usage_baseline=0, status="limited",
        )
        session.add(acc)
        session.commit()
        session.refresh(acc)
        plan = QueuedPlan(
            account_id=acc.id, data_limit_gb=20, duration_days=30,
            status=QueuedPlanStatus.pending, estimated_amount=20 * RATE,
        )
        session.add(plan)
        session.commit()
        session.refresh(plan)
        account_id, plan_id = acc.id, plan.id
        fake.panel["d8_act"] = {"username": "d8_act", "used_traffic": 10 * GB,
                                "status": "limited", "data_limit": 10 * GB}

    gate = Gate()
    fake.modify_gate = gate
    now = datetime.utcnow()

    async def writer_a():
        with Session(engine) as session:
            acc = session.get(Account, account_id)
            plan = session.get(QueuedPlan, plan_id)
            await sync_job._activate_next_plan(session, acc, plan, now)

    task_a = asyncio.create_task(writer_a())
    await asyncio.wait_for(gate.entered.wait(), timeout=10)
    task_b = asyncio.create_task(settle_via_api(account_id))
    # Same two-world choreography as scenario 1.
    done, _ = await asyncio.wait({task_b}, timeout=2.0)
    if task_b not in done:
        gate.release.set()
        await asyncio.wait_for(task_a, timeout=30)
    await asyncio.wait_for(task_b, timeout=30)
    gate.release.set()
    fake.modify_gate = None

    charges = charges_for(account_id)
    # The ended 10 GB package must be charged exactly once. The settle that
    # runs AFTER the activation legitimately invoices the NEW 20 GB package —
    # that is a different sale, not the same GB twice.
    ended = [c for c in charges if c.gb_amount is not None and abs(c.gb_amount - 10.0) < 0.01]
    check("the ended 10 GB plan was charged exactly ONCE (was: twice)",
          len(ended) == 1, f"10GB-charges={len(ended)}, all={[(c.gb_amount, c.amount) for c in charges]}")
    total = sum(c.amount for c in charges)
    check("total charged == ended plan (10,000) + new package sold via settle (20,000)",
          abs(total - 30 * RATE) < 0.01, f"total={total:g}")
    with Session(engine) as session:
        acc = session.get(Account, account_id)
        check("activation landed (new limits, meter zeroed, baseline 0)",
              acc.data_limit == 20 * GB and acc.used_traffic == 0 and acc.usage_baseline == 0,
              f"limit={acc.data_limit}, used={acc.used_traffic}")


async def main() -> None:
    await scenario_cap_hit_vs_settle()
    await scenario_activation_vs_settle()


asyncio.run(main())

print()
if failures:
    print(f"RESULT: {len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("RESULT: sync/settle lock checks OK")
