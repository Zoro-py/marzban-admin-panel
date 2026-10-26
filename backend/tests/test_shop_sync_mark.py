"""The «accounting synced» stamp: a customer's debt is exact only when EVERY
account behind it (own + represented-group members) is stamped.

Plain `python -m tests.test_shop_sync_mark` from `backend/`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="syncmark_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""
os.environ["SHOP_BOT_API_KEY"] = "test-shop-key"
os.environ["DELEGATE_BOT_API_KEY"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account, AppSettings, BillingMode, Customer, Group, LedgerEntry, LedgerSource,
    LedgerType, ShopUser, ShopWalletEntry, ShopWalletEntryType,
)
from app.services import MoneyBook  # noqa: E402

init_db()

failures: list[str] = []


def check(name: str, cond: bool, extra=None):
    print(("  [OK] " if cond else "[FAIL] ") + name + (f"  {extra}" if (extra is not None and not cond) else ""))
    if not cond:
        failures.append(name)


app.dependency_overrides[require_auth] = lambda: "testop"
client = TestClient(app)
BOT_HDR = {"X-Shop-Bot-Key": "test-shop-key"}



def session_of(tg):
    return client.post("/api/shop/bot/session", headers=BOT_HDR, json={"telegram_id": tg}).json()


with Session(engine) as s:
    s.add(AppSettings(id=1, default_rate_per_gb=1000))
    su = ShopUser(telegram_id=777001, display_name="x")
    s.add(su)
    s.flush()
    a_cust = Customer(name="Own", shop_user_id=su.id)
    s.add(a_cust)
    s.flush()
    own = Account(marzban_username="own_acc", customer_id=a_cust.id, billing_mode=BillingMode.prepay)
    s.add(own)
    s.flush()
    s.add(LedgerEntry(type=LedgerType.charge, amount=50000.0, account_id=own.id, customer_id=a_cust.id))
    # a group the customer REPRESENTS, with a member owned by someone else
    other = Customer(name="Mate")
    s.add(other)
    s.flush()
    grp = Group(name="g", representative_customer_id=a_cust.id, billing_mode=BillingMode.payg)
    s.add(grp)
    s.flush()
    mate = Account(marzban_username="mate_acc", customer_id=other.id, group_id=grp.id, billing_mode=BillingMode.payg)
    s.add(mate)
    s.commit()
    OWN, MATE = own.id, mate.id

check("1: nothing stamped -> estimate", session_of(777001)["debt_exact"] is False)

r = client.post(f"/api/accounts/{OWN}/accounting-synced", json={"synced": True})
check("2: stamp endpoint 200 + field set", r.status_code == 200 and r.json()["accounting_synced_at"], r.text[:150])
check("2b: group-mate still unstamped -> still estimate", session_of(777001)["debt_exact"] is False)

client.post(f"/api/accounts/{MATE}/accounting-synced", json={"synced": True})
check("3: every account stamped -> exact", session_of(777001)["debt_exact"] is True)

r = client.post(f"/api/accounts/{MATE}/accounting-synced", json={"synced": False})
check("4: clearing works", r.status_code == 200 and r.json()["accounting_synced_at"] is None)
check("4b: back to estimate", session_of(777001)["debt_exact"] is False)

row = client.get(f"/api/accounts/{OWN}").json()
check("5: AccountRow carries the stamp", row.get("accounting_synced_at"), row.get("accounting_synced_at"))
check("6: unknown account 404", client.post("/api/accounts/99999/accounting-synced", json={"synced": True}).status_code == 404)
with Session(engine) as s:
    check("7: stamp never changes money", MoneyBook(s).customer_net(s.get(Customer, 1)) == 50000.0)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All accounting-sync-mark cases passed.")
