"""reset-cycle on a PREPAY group must refuse cleanly (2026-09-29 checklist
area 2.6 — the row documented as «باید تمیز رد شود»).

RED evidence recorded 2026-09-29 (before the fix, scratch DB): POST
/api/groups/{id}/reset-cycle on a prepay group returned 200 and silently
forgave the whole package — pending 100,000 -> 0, billed_data_limit set to
the full data_limit ("marked billed"), Marzban meter zeroed, ZERO ledger
rows. That is DOMAIN §3's forbidden direction twice over: prepay usage was
zeroed mid-package for free, and unsold inventory was marked invoiced with
no charge anywhere. 7 of the 11 live groups are prepay today, so this is
one accidental click away on real money.

The fix: the endpoint refuses non-payg groups with an explicit message
naming the right paths (settle charges the package; adjust + bill_added_gb
records a package sold outside the ledger; the per-account reset stays the
single-account tool). The payg path is untouched and pinned here too.

Plain `python -m tests.test_group_prepay_reset_refusal` from `backend/`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="prepay_reset_test_")) / "test.db"
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
from app.models import (  # noqa: E402
    Account, AccountRole, AppSettings, BillingMode, Customer, Group, LedgerEntry,
)

init_db()

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class FakeMarzban:
    def __init__(self):
        self.panel: dict[str, dict] = {}
        self.resets: list[str] = []

    async def reset_user(self, username: str) -> dict:
        self.resets.append(username)
        user = self.panel.setdefault(username, {"username": username})
        user["used_traffic"] = 0
        return dict(user)

    async def list_all_users(self) -> list[dict]:
        return []


fake = FakeMarzban()
marzban_module.marzban_client.reset_user = fake.reset_user
marzban_module.marzban_client.list_all_users = fake.list_all_users

app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)
GB = 1024 ** 3

with Session(engine) as session:
    session.add(AppSettings(id=1, default_rate_per_gb=1000))
    cust = Customer(name="Reset Refusal Cust")
    session.add(cust)
    session.commit()
    session.refresh(cust)
    pre = Group(name="Prepay Grp", representative_customer_id=cust.id, billing_mode=BillingMode.prepay)
    payg = Group(name="Payg Grp", representative_customer_id=cust.id, billing_mode=BillingMode.payg)
    session.add(pre)
    session.add(payg)
    session.commit()
    session.refresh(pre)
    session.refresh(payg)
    pre_a = Account(marzban_username="rr_pre", customer_id=cust.id, group_id=pre.id,
                    role=AccountRole.primary, billing_mode=BillingMode.prepay,
                    data_limit=100 * GB, used_traffic=40 * GB, billed_data_limit=0)
    payg_a = Account(marzban_username="rr_payg", customer_id=cust.id, group_id=payg.id,
                     role=AccountRole.primary, billing_mode=BillingMode.payg,
                     data_limit=300 * GB, used_traffic=50 * GB, usage_baseline=0)
    session.add(pre_a)
    session.add(payg_a)
    session.commit()
    session.refresh(pre_a)
    session.refresh(payg_a)
    pre_id, payg_id, pre_acc, payg_acc = pre.id, payg.id, pre_a.id, payg_a.id
    fake.panel["rr_pre"] = {"username": "rr_pre", "used_traffic": 40 * GB, "status": "active"}
    fake.panel["rr_payg"] = {"username": "rr_payg", "used_traffic": 50 * GB, "status": "active"}

# --- the refusal -----------------------------------------------------------
r = client.post(f"/api/groups/{pre_id}/reset-cycle")
check("reset-cycle on a prepay group REFUSES (400)", r.status_code == 400, str(r.status_code))
check("the refusal names the right alternative paths",
      r.status_code == 400 and ("settle" in r.json().get("detail", "").lower()),
      r.json().get("detail", "")[:80])

with Session(engine) as session:
    a = session.get(Account, pre_acc)
    g = session.get(Group, pre_id)
    charges = session.exec(select(LedgerEntry).where(LedgerEntry.type == "charge")).all()
    check("prepay member untouched: billed still 0, meter still 40GB, baseline intact",
          (a.billed_data_limit or 0) == 0 and a.used_traffic == 40 * GB,
          f"billed={a.billed_data_limit}, used={a.used_traffic / GB:g}GB")
    check("group untouched: last_settled_at still null", g.last_settled_at is None)
    check("no charge was invented or needed", len(charges) == 0)
check("Marzban was never called for the refused group", fake.resets == [])

# --- the payg path still works ---------------------------------------------
r = client.post(f"/api/groups/{payg_id}/reset-cycle")
check("reset-cycle on a payg group still works (200)", r.status_code == 200, str(r.status_code))
with Session(engine) as session:
    a = session.get(Account, payg_acc)
    g = session.get(Group, payg_id)
    check("payg member: baseline rolled to the Marzban-reported 0",
          a.usage_baseline == 0 and a.used_traffic == 0,
          f"baseline={a.usage_baseline}, used={a.used_traffic}")
    check("payg group cycle closed", g.last_settled_at is not None)

print()
if failures:
    print(f"RESULT: {len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("RESULT: prepay reset-cycle refusal checks OK")
