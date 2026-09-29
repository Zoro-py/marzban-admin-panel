"""Fleet simulation of the prepay auto-queue (area 3.4, 2026-09-30 round).

Read-only replay of `_maybe_auto_queue_next_plan`'s exact eligibility chain —
importing the REAL functions, not reimplementing them — over a COPY of the
live DB. Sends nothing (the notify step is never reached; red line #6).

    python temp/fleet_sim.py [backend/livecopy_0929.db]
"""
import os
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
db_arg = sys.argv[1] if len(sys.argv) > 1 else "backend/livecopy_0929.db"
db_path = (REPO / db_arg).resolve()
if not db_path.exists():
    sys.exit(f"no such db: {db_path}")

os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "sim")
os.environ.setdefault("MARZBAN_PASSWORD", "sim")
for k in ("BOT_TOKEN", "BOT_ADMIN_CHAT_ID", "SHOP_BOT_TOKEN", "SHOP_BOT_API_KEY",
          "DELEGATE_BOT_TOKEN", "DELEGATE_BOT_API_KEY", "NOTIFY_TELEGRAM_TOKEN"):
    os.environ[k] = ""
sys.path.insert(0, str(REPO / "backend"))

from sqlmodel import Session, select  # noqa: E402
from app.db import engine  # noqa: E402
from app.models import Account, QueuedPlan, QueuedPlanStatus  # noqa: E402
from app.services import BillingMode, effective_billing_mode, monthly_avg_usage  # noqa: E402
from app import sync_job  # noqa: E402

GB = 1024 ** 3
MIN_CYCLE = sync_job.BILLING_MIN_CYCLE_DAYS

with Session(engine) as s:
    now = datetime.utcnow().replace(tzinfo=None)
    accounts = s.exec(select(Account)).all()
    pending = {q.account_id for q in s.exec(
        select(QueuedPlan).where(QueuedPlan.status == QueuedPlanStatus.pending)).all()}
    print(f"db={db_path.name}  accounts={len(accounts)}  pending_plans={len(pending)}")

    eligible = near = insufficient = repeat_branch = 0
    would_queue = []
    heavy_short_cycle = []   # Addendum-6 follow-up (1)
    nonmult5_in_repeat = []  # Addendum-6 follow-up (2)

    for a in accounts:
        if a.status in sync_job._EXCLUDED_STATUSES or not a.auto_renew_enabled:
            continue
        if effective_billing_mode(s, a) != BillingMode.prepay:
            continue
        eligible += 1
        remaining_gb = (a.data_limit - a.used_traffic) / GB if a.data_limit else None
        remaining_days = (a.expire - time.time()) / 86400 if a.expire else None
        nq = remaining_gb is not None and remaining_gb <= sync_job.NEAR_QUOTA_AUTO_QUEUE_REMAINING_GB
        ne = remaining_days is not None and remaining_days <= sync_job.NEAR_EXPIRY_AUTO_QUEUE_REMAINING_DAYS
        if not (nq or ne):
            continue
        near += 1
        avg_gb, _conf, obs_days = monthly_avg_usage(a, now)
        cur_gb = a.data_limit / GB if a.data_limit else None

        # Addendum-6 follow-up (1): package at the CURRENT pace dies in < 5 days
        if avg_gb and cur_gb and (cur_gb / (avg_gb / 30.0)) < MIN_CYCLE:
            heavy_short_cycle.append((a.marzban_username, round(cur_gb, 1), round(avg_gb, 1)))
        # follow-up (2): would land in the repeat branch with a non-multiple-of-5 package
        if avg_gb is not None and obs_days < MIN_CYCLE and cur_gb and cur_gb % 5 != 0:
            nonmult5_in_repeat.append((a.marzban_username, cur_gb))

        if a.id in pending:
            continue
        if avg_gb is None:
            insufficient += 1
            print(f"  INSUFFICIENT_DATA: {a.marzban_username} rem={remaining_gb}GB/{remaining_days}d")
            continue
        damp = sync_job._dampened_package_size_gb(a, avg_gb, obs_days)
        q = sync_job._round_package_size(damp)
        raw = sync_job._round_package_size(avg_gb)
        would_queue.append((a.marzban_username, round(cur_gb or 0, 1), round(avg_gb, 2),
                            round(obs_days, 1), round(damp, 1), q, raw))

    print(f"prepay-eligible(auto_renew on, live status)={eligible}  near-threshold={near}  "
          f"insufficient_data={insufficient}")
    print("would queue now:")
    for u, cur, avg, obs, damp, q, raw in would_queue:
        growth = (q / cur) if cur else 0
        flag = "GROWTH" if q > cur else ("repeat" if obs < MIN_CYCLE else "down/flat")
        print(f"  {u}: cur={cur}GB avg={avg}GB obs={obs}d dampened={damp} -> queue={q}GB "
              f"(raw={raw}) x{growth:.2f} [{flag}]")
    print(f"follow-up(1) heavy-always-short-cycle (<{MIN_CYCLE}d at pace): {heavy_short_cycle or 'NONE'}")
    print(f"follow-up(2) non-mult-of-5 in repeat branch: {nonmult5_in_repeat or 'NONE'}")
