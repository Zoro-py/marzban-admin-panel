"""The «ardani» zero-charge guard (2026-09-29 checklist area 2.8): a 35GB
prepay package once activated with NO charge ("old plan billed 0.0") and the
warning that explained it lived only in a server log that later aged out.
_activate_next_plan must now attach a classified `next_plan_zero_charge`
AccountEvent whenever a prepay old plan resolves to 0, so the reason survives
in the audit trail even when logs don't.

Four reasons, one event shape (pinned here):
  unlimited      — data_limit 0/None at read time (manual invoicing needed)
  ANOMALY        — billed_data_limit > data_limit (over-billed baseline)
  already-billed — billed == data_limit (correct no-double-charge path)
  rate-zero      — effective rate resolved to 0 (comp)

No money moves in any case — the ledger stays empty; that is asserted too.

Plain `python -m tests.test_next_plan_zero_charge_guard` from `backend/`.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="zc_guard_")) / "test.db"
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

from sqlmodel import Session, select  # noqa: E402

import app.sync_job as sync_job  # noqa: E402
from app import services  # noqa: E402
from app import marzban_client as marzban_module  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.models import (  # noqa: E402
    Account, AccountEvent, AccountRole, AppSettings, BillingMode, Customer,
    LedgerEntry, QueuedPlan, QueuedPlanStatus,
)

init_db()

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


GB = 1024 ** 3


class FakeMarzban:
    async def modify_user(self, username, payload):
        return {"username": username, **payload}

    async def reset_user(self, username):
        return {"username": username, "used_traffic": 0}


fake = FakeMarzban()
marzban_module.marzban_client.modify_user = fake.modify_user
marzban_module.marzban_client.reset_user = fake.reset_user

# Notifications must not reach Telegram from a test; the sync job awaits it.
async def _fake_notify(*a, **k):
    return None


sync_job._notify_admin = _fake_notify

with Session(engine) as session:
    session.add(AppSettings(id=1, default_rate_per_gb=1000))
    cust = Customer(name="Zero Charge Guard Cust")
    session.add(cust)
    session.commit()
    session.refresh(cust)
    customer_id = cust.id

CASES = [
    # (label, account kwargs, effective rate, expected detail fragment)
    ("unlimited", dict(billing_mode=BillingMode.prepay, data_limit=None,
                       used_traffic=10 * GB, billed_data_limit=0), 1000.0, "UNLIMITED"),
    ("over-billed ANOMALY", dict(billing_mode=BillingMode.prepay, data_limit=10 * GB,
                                 used_traffic=5 * GB, billed_data_limit=12 * GB), 1000.0, "ANOMALY"),
    ("already fully billed", dict(billing_mode=BillingMode.prepay, data_limit=10 * GB,
                                  used_traffic=5 * GB, billed_data_limit=10 * GB), 1000.0,
     "already fully billed"),
    ("rate zero", dict(billing_mode=BillingMode.prepay, data_limit=35 * GB,
                       used_traffic=5 * GB, billed_data_limit=0), 0.0, "rate resolved to 0"),
]


async def run_case(label: str, acc_kwargs: dict, rate: float, expected: str) -> None:
    print(f"case: {label}")
    with Session(engine) as session:
        a = Account(marzban_username=f"zc_{abs(hash(label)) % 100000}", customer_id=customer_id,
                    role=AccountRole.primary, **acc_kwargs)
        session.add(a)
        session.commit()
        session.refresh(a)
        plan = QueuedPlan(account_id=a.id, data_limit_gb=20, duration_days=30,
                          status=QueuedPlanStatus.pending, estimated_amount=20000)
        session.add(plan)
        session.commit()
        session.refresh(plan)
        aid, pid = a.id, plan.id

    with Session(engine) as session:
        a = session.get(Account, aid)
        plan = session.get(QueuedPlan, pid)
        orig = sync_job.effective_rate
        sync_job.effective_rate = (lambda session, account, group=None, _r=rate: _r)
        try:
            await sync_job._activate_next_plan(session, a, plan, datetime.utcnow())
        finally:
            sync_job.effective_rate = orig

    with Session(engine) as session:
        events = session.exec(select(AccountEvent).where(
            AccountEvent.account_id == aid,
            AccountEvent.action == "next_plan_zero_charge")).all()
        check(f"{label}: classified zero-charge event written",
              len(events) == 1 and expected in events[0].detail,
              events[0].detail[:60] if events else "NO EVENT")
        charges = session.exec(select(LedgerEntry).where(
            LedgerEntry.account_id == aid, LedgerEntry.type == "charge")).all()
        check(f"{label}: no money invented (ledger untouched by the guard)",
              len(charges) == 0, f"charges={len(charges)}")
        plan_state = session.get(QueuedPlan, pid)
        check(f"{label}: plan still activated normally", plan_state.status == QueuedPlanStatus.activated)


async def main():
    for label, kwargs, rate, expected in CASES:
        await run_case(label, kwargs, rate, expected)


asyncio.run(main())

print()
if failures:
    print(f"RESULT: {len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("RESULT: next-plan zero-charge guard checks OK")
