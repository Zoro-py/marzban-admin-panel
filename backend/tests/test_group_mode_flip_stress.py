"""D21 repeated-toggle stress (2026-09-29 checklist area 2.6): the mode-flip
guards were unit-tested for ONE flip each (test_billed_baseline_mode_flip.py);
this pins the OPERATOR shape — toggling a group's mode back and forth across
several flips with a payg-shaped member inside.

Invariants across all flips:
- every payg→prepay flip neutralizes the baseline (billed == data_limit),
  so owes-now never shows the phantom package;
- every prepay→payg flip leaves the field alone (inert under payg — the
  290,000 the row then shows is the member's REAL metered pending, not a
  phantom);
- no ledger row is ever written by a mode flip (D21: "اثر پولی صفر").

Plain `python -m tests.test_group_mode_flip_stress` from `backend/`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="flip_stress_")) / "test.db"
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

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import marzban_client as marzban_module  # noqa: E402
from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, AccountRole, AppSettings, BillingMode, Customer, Group, LedgerEntry  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class FakeMarzban:
    async def list_all_users(self):
        return []

    async def reset_user(self, username):
        return {"username": username, "used_traffic": 0}


marzban_module.marzban_client.list_all_users = FakeMarzban().list_all_users
app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)
GB = 1024 ** 3

with Session(engine) as session:
    session.add(AppSettings(id=1, default_rate_per_gb=1000))
    cust = Customer(name="Flip Stress Cust")
    session.add(cust)
    session.commit()
    session.refresh(cust)
    g = Group(name="Flip Stress Grp", representative_customer_id=cust.id,
              billing_mode=BillingMode.payg)
    session.add(g)
    session.commit()
    session.refresh(g)
    # Payg-shaped member: 300GB Marzban cap, stale prepay leftover 150GB,
    # 290GB metered this cycle.
    a = Account(marzban_username="flip1", group_id=g.id, role=AccountRole.primary,
                billing_mode=BillingMode.prepay, data_limit=300 * GB,
                used_traffic=290 * GB, billed_data_limit=150 * GB, usage_baseline=0)
    session.add(a)
    session.commit()
    session.refresh(a)
    gid, account_id = g.id, a.id  # plain ints — `a` detaches when this session closes

for i, mode in enumerate(["prepay", "payg", "prepay", "payg", "prepay"]):
    r = client.patch(f"/api/groups/{gid}", json={"billing_mode": mode})
    check(f"flip {i + 1} -> {mode} accepted", r.status_code == 200, str(r.status_code))
    with Session(engine) as session:
        acc = session.get(Account, account_id)
        billed_gb = (acc.billed_data_limit or 0) / GB
        row = client.get("/api/accounts", params={"group_id": gid}).json()[0]
        if mode == "prepay":
            check(f"flip {i + 1}: payg→prepay neutralized (no phantom)",
                  abs(billed_gb - 300.0) < 0.01 and row["net_owed"] <= 0.01,
                  f"billed={billed_gb:g}GB, owes={row['net_owed']}")
        else:
            check(f"flip {i + 1}: prepay→payg inert, real metered pending shows",
                  abs(row["net_owed"] - 290000.0) < 0.01,
                  f"owes={row['net_owed']} (= 290GB × 1000 real payg usage)")

with Session(engine) as session:
    rows = session.exec(select(LedgerEntry)).all()
    check("no ledger row was ever written by the mode flips", len(rows) == 0,
          f"rows={len(rows)}")

print()
if failures:
    print(f"RESULT: {len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("RESULT: group mode-flip stress checks OK")
