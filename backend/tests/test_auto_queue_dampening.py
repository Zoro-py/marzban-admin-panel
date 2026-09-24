"""Reproduces the 2026-09 prepay over-sizing defect in sync_job.py's
_maybe_auto_queue_next_plan, and checks the dampening that fixes it.

Bug report: monthly_avg_usage's current-cycle branch linearly extrapolates
whatever pace it has seen once 0.5 days have elapsed — right for the
dashboard's cosmetic "Monthly average", wrong for a billing decision, since
prepay bills the FULL queued package size at settlement regardless of actual
consumption. Two live accounts got mis-sized that way: a 20 GB package
burned in ~2.89 days extrapolated to ~197 GB/mo and auto-queued a 195 GB
package (9.75x the size that was ending), and a 65 GB package burned in
~8.4 days queued 225 GB off a ~228 GB/mo extrapolation. The fix dampens the
estimate in _dampened_package_size_gb: under BILLING_MIN_CYCLE_DAYS of
observation the current package size is repeated unchanged, otherwise growth
is capped at MAX_GROWTH_MULTIPLE of it.

Plain `python -m tests.test_auto_queue_dampening` from `backend/`.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="auto_queue_dampening_test_")) / "test.db"
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
from app.models import Account, AccountEvent, Customer, QueuedPlan, QueuedPlanStatus, utcnow  # noqa: E402
from app.sync_job import (_dampened_package_size_gb, _round_package_size,  # noqa: E402
                          run_sync)

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

# Auto-queue is notify-first (nothing is queued unless the admin notification
# succeeds — DOMAIN_AND_BILLING.md §4.1), and the real notify_admin RAISES
# with BOT_TOKEN empty, which would abort the queue before any write. Capture
# sends instead; assertions below read the captured texts.
sent: list[str] = []


async def fake_notify(text: str, reply_markup: dict | None = None) -> None:
    sent.append(text)


sync_job._notify_admin = fake_notify


def seed_account(username: str, *, data_limit_gb: float, used_gb: float,
                 baseline_gb: float, baseline_days_ago: float) -> int:
    """One prepay account mid-package, with usage_baseline set so
    monthly_avg_usage takes its current-cycle branch at exactly the pace
    under test: (used_gb - baseline_gb) consumed over baseline_days_ago."""
    now_naive = utcnow().replace(tzinfo=None)
    expire = int(time.time() + 20 * 86400)  # comfortably far: near-quota is the trigger under test, not near-expiry
    with Session(engine) as session:
        customer = Customer(name=f"{username} Customer")
        session.add(customer)
        session.commit()
        session.refresh(customer)
        account = Account(
            marzban_username=username,
            customer_id=customer.id,
            status="active",
            used_traffic=int(used_gb * GB),
            lifetime_used_traffic=int(used_gb * GB),
            first_seen_traffic=0,
            first_seen_traffic_at=now_naive - timedelta(days=baseline_days_ago + 40),
            usage_baseline=int(baseline_gb * GB),
            usage_baseline_at=now_naive - timedelta(days=baseline_days_ago),
            data_limit=int(data_limit_gb * GB),
            expire=expire,
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        # Marzban mirrors the seeded local state exactly, so sync's own
        # change-detection (external reset, plan change) stays quiet and the
        # only path this run exercises is the auto-queue itself.
        fake.users[username] = {
            "username": username,
            "used_traffic": int(used_gb * GB),
            "lifetime_used_traffic": int(used_gb * GB),
            "data_limit": int(data_limit_gb * GB),
            "expire": expire,
            "status": "active",
            "subscription_url": "/sub/tok",
        }
        return account.id


# ---- Live case 1 (Mansoorizade, account id=6): a 20 GB package with 19 GB
# consumed in ~2.89 days — a one-time burst. Raw monthly_avg_usage read
# ~197 GB/mo (19 GB / 2.89 d * 30) and the old code queued 195 GB, a 9.75x
# jump. 2.89 < BILLING_MIN_CYCLE_DAYS, so the fix repeats the current 20 GB
# package unchanged instead of extrapolating at all. ----
mansoor_id = seed_account("mansoorizade-test", data_limit_gb=20, used_gb=19.5,
                          baseline_gb=0.5, baseline_days_ago=2.89)
asyncio.run(run_sync())

with Session(engine) as session:
    plan = session.exec(select(QueuedPlan).where(
        QueuedPlan.account_id == mansoor_id,
        QueuedPlan.status == QueuedPlanStatus.pending,
    )).first()
    check("Mansoorizade case: a plan was queued", plan is not None)
    check(f"Mansoorizade case: size is the repeated 20 GB package, not the 195 GB extrapolation (got {plan.data_limit_gb:g})",
          plan is not None and plan.data_limit_gb == 20)
    event = session.exec(select(AccountEvent).where(
        AccountEvent.account_id == mansoor_id,
        AccountEvent.action == "next_plan_auto_queued",
    )).first()
    check("Mansoorizade case: audit event still logged under the same action", event is not None)
    check("Mansoorizade case: audit event records raw vs dampened ('dampened from raw 195 GB')",
          event is not None and "dampened from raw 195 GB" in event.detail)
    check("Mansoorizade case: admin notification says the size was limited",
          any("محدود شد" in t for t in sent))
    check("Mansoorizade case: customer-forward message dropped the equals-the-average claim",
          any("معادل میانگین" not in t and "شارژ کردم" in t for t in sent))

# ---- Live case 2 (account id=71): a 65 GB package with 64 GB consumed in
# ~8.4 days — above BILLING_MIN_CYCLE_DAYS, so the pace IS trusted, but
# growth is capped at 65 * MAX_GROWTH_MULTIPLE = 130 GB instead of the raw
# ~228 GB/mo extrapolation that queued 225 GB. ----
acc71_id = seed_account("acc71-test", data_limit_gb=65, used_gb=64.2,
                        baseline_gb=0.2, baseline_days_ago=8.4)
asyncio.run(run_sync())

with Session(engine) as session:
    plan = session.exec(select(QueuedPlan).where(
        QueuedPlan.account_id == acc71_id,
        QueuedPlan.status == QueuedPlanStatus.pending,
    )).first()
    check("account-71 case: a plan was queued", plan is not None)
    check(f"account-71 case: growth capped at 130 GB, not the 225 GB extrapolation (got {plan.data_limit_gb:g})",
          plan is not None and plan.data_limit_gb == 130)
    event = session.exec(select(AccountEvent).where(
        AccountEvent.account_id == acc71_id,
        AccountEvent.action == "next_plan_auto_queued",
    )).first()
    check("account-71 case: audit event records raw vs dampened ('dampened from raw 225 GB')",
          event is not None and "dampened from raw 225 GB" in event.detail)

# ---- Regression: GENUINE gradual growth — account 71's own real 35 -> 55
# -> 65 GB history — must pass through untouched. Each step's pace is a
# plausible ~14-day cycle whose monthly rate lands well under 2x the current
# package, which is what steady growth actually looks like; dampening must
# bite on bursts, not on this. ----
growth_steps = [
    # (current package GB, consumed GB, observed days, expected queue GB)
    (35, 26.0, 14.2, 55),  # 26/14.2*30 ≈ 54.9 GB/mo -> 55 GB next package
    (55, 30.0, 14.0, 65),  # 30/14*30 ≈ 64.3 GB/mo -> 65 GB next package
    (65, 33.0, 15.0, 65),  # 33/15*30 = 66 GB/mo -> steady state holds at 65
]
for current_gb, consumed_gb, days, expected in growth_steps:
    avg_gb = round(consumed_gb / days * 30, 2)
    step = Account(marzban_username="growth-step", data_limit=int(current_gb * GB))
    dampened = _dampened_package_size_gb(step, avg_gb, days)
    check(f"gradual growth {current_gb} -> {expected} GB (avg {avg_gb:g}): no dampening applied",
          dampened == avg_gb)
    check(f"gradual growth {current_gb} -> {expected} GB: rounds to {expected}, not throttled to the {current_gb * 2:g} GB cap",
          _round_package_size(dampened) == expected)

# ---- A comfortably-long sample whose estimate is already reasonable must
# come through UNCHANGED — dampening caps bursts; it is not a blanket
# reduction. 12 observed days at 45 GB/mo against a 50 GB package. ----
reasonable = Account(marzban_username="reasonable-case", data_limit=int(50 * GB))
check("reasonable estimate passes through untouched (45 GB/mo, 50 GB package)",
      _dampened_package_size_gb(reasonable, 45.0, 12.0) == 45.0)
check("reasonable estimate still rounds the same way (45 GB)",
      _round_package_size(_dampened_package_size_gb(reasonable, 45.0, 12.0)) == 45.0)

# ---- Insufficient data (avg_gb is None) is entirely unaffected: no plan
# queued, no crash, and the same hands-off "set the next plan by hand"
# notification as before. No usage_baseline_at (legacy row) and first seen
# 1 day ago — under services.MIN_USAGE_SAMPLE_DAYS, so monthly_avg_usage
# returns None rather than guessing. ----
sent.clear()
with Session(engine) as session:
    customer = Customer(name="No-History Customer")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    fresh = Account(
        marzban_username="no-history-test",
        customer_id=customer.id,
        status="active",
        used_traffic=int(9.5 * GB),   # 0.5 GB left -> near-quota fires
        data_limit=int(10 * GB),
        first_seen_traffic=0,
        first_seen_traffic_at=utcnow().replace(tzinfo=None) - timedelta(days=1),
        expire=int(time.time() + 20 * 86400),
    )
    session.add(fresh)
    session.commit()
    session.refresh(fresh)
    asyncio.run(sync_job._maybe_auto_queue_next_plan(session, fresh, utcnow()))
    plan = session.exec(select(QueuedPlan).where(
        QueuedPlan.account_id == fresh.id,
        QueuedPlan.status == QueuedPlanStatus.pending,
    )).first()
    check("insufficient-data case: no plan queued", plan is None)
    check(f"insufficient-data case: same single hands-off admin notification (got {len(sent)})",
          len(sent) == 1 and "دستی" in sent[0])

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All auto-queue dampening cases passed.")
