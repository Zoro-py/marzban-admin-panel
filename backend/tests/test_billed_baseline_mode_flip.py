"""Reproduces and locks the Mahan trap (2026-09-28): a payg-shaped account
carries a stale billed_data_limit (payg never reads it, so nothing kept it
current — the payg standard-shape sweep left old prepay leftovers under the
300GB Marzban cap). The moment its EFFECTIVE billing mode flips payg→prepay,
prepay's billable_bytes (data_limit - billed_data_limit) reads that leftover
as hundreds of GB of phantom "sold but uninvoiced" package debt. Live case:
Mahan (id=8) jumped ~154k → ~1,425k owes-now on one group assignment.

The fix: `services.neutralize_billed_baseline` (the exact one line every
prepay settle already writes: `billed_data_limit = data_limit or 0`) called
from two guards — update_relationship when joining/moving into a prepay
billing context, and update_group when a group itself flips payg→prepay.
Only the payg→prepay direction is guarded; prepay→payg is inert (payg never
reads billed_data_limit).

Also proves the pre-existing paths that write the same pattern
(settle_account, settle_group, next-plan activation) are untouched, and that
nothing here creates a LedgerEntry — the neutralization is not money.

Plain `python -m tests.test_billed_baseline_mode_flip` from `backend/`, same
harness shape as tests/test_payg_shape.py.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="billed_flip_test_")) / "test.db"
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

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import marzban_client as marzban_module  # noqa: E402
from app import sync_job  # noqa: E402
from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    AccountEvent,
    AppSettings,
    BillingMode,
    Customer,
    Group,
    LedgerEntry,
    QueuedPlan,
    utcnow,
)
from app.services import GB, billable_bytes, effective_billing_mode  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


def neutralize_events(account_id: int) -> list[AccountEvent]:
    with Session(engine) as s:
        return list(s.exec(
            select(AccountEvent).where(
                AccountEvent.account_id == account_id,
                AccountEvent.action == "billed_baseline_neutralized",
            )
        ).all())


# Assigned straight onto the marzban_client INSTANCE — instance attributes
# don't bind methods (same trick as tests/test_payg_shape.py). Only the
# next-plan activation regression test actually exercises these.
async def _fake_modify_user(username: str, payload: dict) -> dict:
    return {"username": username, "expire": payload.get("expire"), "data_limit": payload.get("data_limit"), "status": "active"}


async def _fake_reset_user(username: str) -> dict:
    return {"username": username, "used_traffic": 0}


marzban_module.marzban_client.modify_user = _fake_modify_user
marzban_module.marzban_client.reset_user = _fake_reset_user


async def _noop_notify(message: str) -> None:
    pass


sync_job._notify_admin = _noop_notify

app.dependency_overrides[require_auth] = lambda: "flip-operator"
client = TestClient(app)

PAYG_LEFTOVER = 15 * GB
CAP_300 = 300 * GB

with Session(engine) as session:
    session.add(AppSettings(id=1, default_rate_per_gb=5000))
    cust = Customer(name="Flip Customer")
    cust_grp = Customer(name="Flip Group Rep")
    cust_safe = Customer(name="Flip Safe Customer")
    session.add_all([cust, cust_grp, cust_safe])
    session.commit()
    session.refresh(cust)
    session.refresh(cust_grp)
    session.refresh(cust_safe)

    # The Mahan shape: standalone payg account, swept to the 300GB Marzban
    # cap, stale prepay leftover in billed_data_limit, small real usage.
    mahan = Account(
        marzban_username="flip-mahan", customer_id=cust.id, billing_mode=BillingMode.payg,
        data_limit=CAP_300, billed_data_limit=PAYG_LEFTOVER,
        used_traffic=2 * GB, usage_baseline=0,
    )
    # The group Mahan joins in test 1: created prepay (the live case).
    prepay_grp = Group(name="Flip Prepay Group", representative_customer_id=cust_grp.id, billing_mode=BillingMode.prepay)
    # Guard-2 group: already payg, two live payg-shaped members + one
    # soft-deleted one (deleted members must stay untouched).
    flip_grp = Group(name="Flip Group", representative_customer_id=cust_grp.id, billing_mode=BillingMode.payg)
    # Guard-1 safe direction: a real prepay package with real pending.
    safe_acct = Account(
        marzban_username="flip-safe", customer_id=cust_safe.id, billing_mode=BillingMode.prepay,
        data_limit=40 * GB, billed_data_limit=10 * GB,
        used_traffic=1 * GB, usage_baseline=0,
    )
    payg_grp = Group(name="Flip Payg Group", representative_customer_id=cust_grp.id, billing_mode=BillingMode.payg)
    # Regression 4a: standalone prepay settle path.
    settle_acct = Account(
        marzban_username="flip-settle", customer_id=cust_safe.id, billing_mode=BillingMode.prepay,
        data_limit=42 * GB, billed_data_limit=0,
    )
    # Regression 4c: next-plan activation path.
    act_acct = Account(
        marzban_username="flip-activate", customer_id=cust_safe.id, billing_mode=BillingMode.prepay,
        data_limit=30 * GB, billed_data_limit=25 * GB,
        used_traffic=25 * GB, usage_baseline=0,
    )
    session.add_all([mahan, prepay_grp, flip_grp, safe_acct, payg_grp, settle_acct, act_acct])
    session.commit()
    for obj in (mahan, prepay_grp, flip_grp, safe_acct, payg_grp, settle_acct, act_acct):
        session.refresh(obj)

    m1 = Account(marzban_username="flip-m1", group_id=flip_grp.id, customer_id=cust_grp.id, billing_mode=BillingMode.prepay,
                 data_limit=CAP_300, billed_data_limit=PAYG_LEFTOVER, used_traffic=3 * GB, usage_baseline=0)
    m2 = Account(marzban_username="flip-m2", group_id=flip_grp.id, customer_id=cust_grp.id, billing_mode=BillingMode.prepay,
                 data_limit=CAP_300, billed_data_limit=PAYG_LEFTOVER, used_traffic=4 * GB, usage_baseline=0)
    m_dead = Account(marzban_username="flip-m-dead", group_id=flip_grp.id, customer_id=cust_grp.id, billing_mode=BillingMode.prepay,
                     data_limit=CAP_300, billed_data_limit=PAYG_LEFTOVER, used_traffic=0, usage_baseline=0)
    session.add_all([m1, m2, m_dead])
    session.commit()
    for obj in (m1, m2, m_dead):
        session.refresh(obj)
    m_dead.deleted_at = utcnow()
    session.add(m_dead)
    session.commit()

    # Regression 4b: prepay group settle path.
    prepay_grp2 = Group(name="Flip Prepay Group 2", representative_customer_id=cust_grp.id, billing_mode=BillingMode.prepay)
    session.add(prepay_grp2)
    session.commit()
    session.refresh(prepay_grp2)
    gm = Account(marzban_username="flip-gm", group_id=prepay_grp2.id, customer_id=cust_grp.id, billing_mode=BillingMode.prepay,
                 data_limit=40 * GB, billed_data_limit=0)
    session.add(gm)
    session.commit()
    session.refresh(gm)

    plan = QueuedPlan(account_id=act_acct.id, data_limit_gb=20.0, duration_days=31, billing_mode=None)
    session.add(plan)
    session.commit()
    session.refresh(plan)

    ids = {
        "cust": cust.id, "cust_grp": cust_grp.id,
        "mahan": mahan.id, "prepay_grp": prepay_grp.id, "flip_grp": flip_grp.id,
        "m1": m1.id, "m2": m2.id, "m_dead": m_dead.id,
        "safe": safe_acct.id, "payg_grp": payg_grp.id,
        "settle": settle_acct.id,
        "prepay_grp2": prepay_grp2.id, "gm": gm.id,
        "act": act_acct.id, "plan": plan.id,
    }

# ── 1) The Mahan case, exactly: payg-shaped standalone → prepay group ──────
with Session(engine) as session:
    a = session.get(Account, ids["mahan"])
    mode = effective_billing_mode(session, a)
    check("1a. pre-condition: standalone effective mode is payg", mode == BillingMode.payg)
    check("1b. pre-condition: payg billable is usage-based (2GB), not package-based",
          billable_bytes(a, mode) == 2 * GB)

r = client.patch(f"/api/accounts/{ids['mahan']}/relationship", json={"group_id": ids["prepay_grp"]})
check("1c. relationship PATCH into the prepay group succeeds", r.status_code == 200)

with Session(engine) as session:
    a = session.get(Account, ids["mahan"])
    mode = effective_billing_mode(session, a)
    check("1d. effective mode is now the group's prepay", mode == BillingMode.prepay)
    check("1e. billed_data_limit was neutralized to data_limit (300GB)",
          a.billed_data_limit == CAP_300)
    check("1f. prepay billable is ~0 — NOT the phantom 285GB",
          billable_bytes(a, mode) == 0)
check("1g. exactly one billed_baseline_neutralized audit event", len(neutralize_events(ids["mahan"])) == 1)
with Session(engine) as session:
    rel_events = list(session.exec(
        select(AccountEvent).where(AccountEvent.account_id == ids["mahan"], AccountEvent.action == "relationship_change")
    ).all())
check("1h. the pre-existing relationship_change event still fires", len(rel_events) == 1)
ev = neutralize_events(ids["mahan"])
check("1i. event detail records the GB before/after (15 -> 300)",
      bool(ev) and "15.000" in ev[0].detail and "300.000" in ev[0].detail)

# A customer-only relationship PATCH cannot flip the mode — no new event.
r = client.patch(f"/api/accounts/{ids['mahan']}/relationship", json={"customer_id": ids["cust_grp"]})
check("1j. customer-only re-PATCH succeeds", r.status_code == 200)
check("1k. customer-only PATCH writes no second neutralize event", len(neutralize_events(ids["mahan"])) == 1)

# ── 2) The other dangerous direction: the group itself flips ───────────────
r = client.patch(f"/api/groups/{ids['flip_grp']}", json={"billing_mode": "prepay"})
check("2a. group payg->prepay PATCH succeeds", r.status_code == 200)
with Session(engine) as session:
    m1_after = session.get(Account, ids["m1"])
    m2_after = session.get(Account, ids["m2"])
    m_dead_after = session.get(Account, ids["m_dead"])
    check("2b. live member m1 neutralized", m1_after.billed_data_limit == CAP_300)
    check("2c. live member m2 neutralized", m2_after.billed_data_limit == CAP_300)
    check("2d. soft-deleted member NOT touched", m_dead_after.billed_data_limit == PAYG_LEFTOVER)
check("2e. one neutralize event per live member", len(neutralize_events(ids["m1"])) == 1 and len(neutralize_events(ids["m2"])) == 1)
check("2f. soft-deleted member got no event", len(neutralize_events(ids["m_dead"])) == 0)

# The safe direction is a no-op: flipping back prepay->payg writes nothing.
events_before = len(neutralize_events(ids["m1"]))
r = client.patch(f"/api/groups/{ids['flip_grp']}", json={"billing_mode": "payg"})
check("2g. group prepay->payg PATCH succeeds", r.status_code == 200)
with Session(engine) as session:
    g = session.get(Group, ids["flip_grp"])
    m1_back = session.get(Account, ids["m1"])
    check("2h. group is payg again", g.billing_mode == BillingMode.payg)
    check("2i. m1's billed_data_limit untouched by the reverse flip", m1_back.billed_data_limit == CAP_300)
check("2j. reverse flip wrote no neutralize event", len(neutralize_events(ids["m1"])) == events_before)

# ── 3) Safe direction for guard 1: real prepay package joins a payg group ──
with Session(engine) as session:
    pre = session.get(Account, ids["safe"]).billed_data_limit
r = client.patch(f"/api/accounts/{ids['safe']}/relationship", json={"group_id": ids["payg_grp"]})
check("3a. prepay account joins the payg group", r.status_code == 200)
with Session(engine) as session:
    a = session.get(Account, ids["safe"])
    mode = effective_billing_mode(session, a)
    check("3b. effective mode is payg (group wins)", mode == BillingMode.payg)
    check("3c. its REAL pending package (10GB billed of 40GB) was NOT neutralized",
          a.billed_data_limit == 10 * GB and pre == 10 * GB)
    check("3d. payg billable is usage-based (1GB)", billable_bytes(a, mode) == 1 * GB)
check("3e. no neutralize event on the safe direction", len(neutralize_events(ids["safe"])) == 0)

# ── 4) No regression in the paths that already wrote this pattern ─────────
# 4a. settle_account (routers/accounts.py — `billed_data_limit = data_limit or 0`).
r = client.post(f"/api/accounts/{ids['settle']}/settle", json={})
check("4a. settle_account succeeds", r.status_code == 200)
check("4a. charges the whole 42GB package at the 5000 rate (210,000)",
      r.json().get("charged_amount") == 210000.0)
with Session(engine) as session:
    a = session.get(Account, ids["settle"])
    check("4a. settle still marks the package billed", a.billed_data_limit == 42 * GB)
check("4a. settle writes its own settle_reset event, not a neutralize event",
      len(neutralize_events(ids["settle"])) == 0)

# 4b. settle_group (routers/groups.py — same pattern per member).
r = client.post(f"/api/groups/{ids['prepay_grp2']}/settle", json={})
check("4b. settle_group succeeds", r.status_code == 200)
with Session(engine) as session:
    gm = session.get(Account, ids["gm"])
    check("4b. group settle still marks the member's package billed", gm.billed_data_limit == 40 * GB)
check("4b. group settle writes no neutralize event", len(neutralize_events(ids["gm"])) == 0)

# 4c. next-plan activation (sync_job._activate_next_plan — sets billed 0).
with Session(engine) as session:
    plan = session.get(QueuedPlan, ids["plan"])
    acct = session.get(Account, ids["act"])
    asyncio.run(sync_job._activate_next_plan(session, acct, plan, utcnow()))
with Session(engine) as session:
    plan = session.get(QueuedPlan, ids["plan"])
    acct = session.get(Account, ids["act"])
    check("4c. plan activated", plan.status.value == "activated")
    check("4c. activation resets the new plan's baseline to 0 (unchanged behaviour)",
          acct.billed_data_limit == 0)
    check("4c. new data_limit applied (20GB)", acct.data_limit == 20 * GB)
    events = list(session.exec(
        select(AccountEvent).where(AccountEvent.account_id == ids["act"], AccountEvent.action == "next_plan_activated")
    ).all())
    check("4c. next_plan_activated event intact", len(events) == 1)
check("4c. activation wrote no neutralize event", len(neutralize_events(ids["act"])) == 0)

# ── 5) The red line: none of the above created a single LedgerEntry ────────
with Session(engine) as session:
    ledger_rows = list(session.exec(select(LedgerEntry)).all())
    settled_ledger = [e for e in ledger_rows if e.account_id in (ids["settle"], ids["gm"], ids["act"])]
    flipped_ledger = [e for e in ledger_rows if e.account_id in (ids["mahan"], ids["m1"], ids["m2"], ids["safe"], ids["m_dead"])]
check("5a. ledger rows exist ONLY from the deliberate 4a/4b/4c settles",
      len(ledger_rows) == len(settled_ledger))
check("5b. the mode-flip neutralization itself created ZERO ledger rows",
      len(flipped_ledger) == 0)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All billed-baseline mode-flip cases passed.")
