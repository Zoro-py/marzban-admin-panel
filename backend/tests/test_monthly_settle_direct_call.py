"""Regression test for the monthly payg settlement's direct settle calls.

The monthly job calls settle_group()/settle_account() directly — outside
FastAPI — so the endpoints' `operator: str = Depends(require_auth)` default
binds to the raw Depends object, and the first `created_by=operator` write
blows up at commit time ("Error binding parameter: type 'Depends' is not
supported"). Every entity fails, gets rolled back — and the month is then
marked as settled anyway. Found by the 2026-09-22 dry-run: settled=0,
failed=15.

This test runs the REAL monthly pipeline against a fresh DB with a faked
Marzban and a faked Telegram, and requires that every entity actually
settles, that the posted charges carry an explicit system operator
attribution, and that a direct settle call without an explicit operator
fails LOUDLY instead of silently losing the whole batch.

Plain `python -m tests.test_monthly_settle_direct_call` from `backend/` —
same harness shape as the rest of this directory (no pytest).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="monthly_settle_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jdatetime  # noqa: E402

from app import marzban_client as marzban_module  # noqa: E402
from app import payg_monthly_job as job  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    BillingMode,
    Customer,
    Group,
    LedgerEntry,
    MonthlySettlementBatch,
)
from app.routers.accounts import settle_account  # noqa: E402
from app.services import get_settings  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


RESET_CALLS: list[str] = []
NOTIFIED: list[str] = []


async def fake_reset_user(username: str) -> dict:
    RESET_CALLS.append(username)
    return {"username": username, "used_traffic": 0, "status": "active"}


async def fake_notify(text: str) -> None:
    NOTIFIED.append(text)


def seed() -> None:
    with Session(engine) as s:
        rep = Customer(name="monthly-test-rep")
        standalone_cust = Customer(name="monthly-test-standalone")
        s.add(rep)
        s.add(standalone_cust)
        s.flush()
        g = Group(name="monthly-test-group", representative_customer_id=rep.id,
                  billing_mode=BillingMode.payg, rate_per_gb=5000.0)
        s.add(g)
        s.flush()
        for i, used in enumerate((10 * 1024**3, 20 * 1024**3)):
            s.add(Account(marzban_username=f"monthly_member_{i}", group_id=g.id,
                          customer_id=rep.id, billing_mode=BillingMode.payg,
                          used_traffic=used, usage_baseline=0, status="active",
                          data_limit=100 * 1024**3))
        s.add(Account(marzban_username="monthly_standalone", customer_id=standalone_cust.id,
                      billing_mode=BillingMode.payg, used_traffic=5 * 1024**3,
                      usage_baseline=0, status="active", rate_per_gb=5000.0))
        s.commit()
        # the live marker: the month before the fake "today" is already done
        settings = get_settings(s)
        settings.last_payg_monthly_settlement = "1405-05"
        s.add(settings)
        s.commit()


async def main() -> None:
    seed()
    job.notify_admin = fake_notify
    marzban_module.marzban_client.reset_user = fake_reset_user

    # fake "today" = the last day of Shahrivar 1405 (22 Sep 2026)
    real_today = jdatetime.date.today
    jdatetime.date.today = classmethod(
        lambda cls: jdatetime.date.fromgregorian(date=datetime(2026, 9, 22).date()))
    try:
        result = await job.maybe_run_monthly_payg_settlement()
    finally:
        jdatetime.date.today = real_today

    print("monthly pipeline result:", result)
    check("pipeline ran for 1405-06", result.get("period") == "1405-06" and result.get("ran") is True)
    check("every entity settled (no silent failures)",
          result.get("settled") == 2 and result.get("failed") == 0, str(result))
    check("both group members' Marzban reset attempted",
          sorted(RESET_CALLS) == ["monthly_member_0", "monthly_member_1", "monthly_standalone"],
          str(RESET_CALLS))

    with Session(engine) as s:
        charges = s.exec(select(LedgerEntry).where(LedgerEntry.type == "charge")).all()
        check("3 charges posted (one per member)", len(charges) == 3)
        check("charges carry explicit operator attribution",
              all(c.created_by == "system:payg-monthly" for c in charges),
              str({c.created_by for c in charges}))
        check("charge amounts = usage x rate",
              sorted(c.amount for c in charges) == [25000.0, 50000.0, 100000.0],
              str(sorted(c.amount for c in charges)))
        batches = s.exec(select(MonthlySettlementBatch).where(
            MonthlySettlementBatch.jalali_period == "1405-06")).all()
        check("2 batch rows recorded (one per group, one per standalone account)",
              len(batches) == 2)
        check("marker advanced to 1405-06", get_settings(s).last_payg_monthly_settlement == "1405-06")
        members = s.exec(select(Account).where(Account.marzban_username.contains("monthly_"))).all()
        check("baselines rolled to post-reset meter (0) — no double bill next month",
              all(a.usage_baseline == 0 for a in members))

    # direct call without an explicit operator must fail LOUDLY, not silently
    # roll back the batch at commit time
    try:
        await settle_account(1, type("R", (), {"mark_paid": False, "pay_scope": "full"})(),
                             Session(engine))
        loud = False
    except RuntimeError:
        loud = True
    except Exception:
        loud = False
    check("direct settle call without operator raises RuntimeError (guard)", loud)

    print()
    if failures:
        print(f"RESULT: {len(failures)} FAILURES: {failures}")
        sys.exit(1)
    print("RESULT: all checks OK")


asyncio.run(main())
