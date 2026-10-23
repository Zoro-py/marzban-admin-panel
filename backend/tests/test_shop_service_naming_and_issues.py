"""Two owner features, 2026-10-23 evening:
1. Service nicknames — the customer names their own service through the shop
   bot (Account.service_name); ownership is the bot_list_accounts scoping, an
   AccountEvent records every rename, and the bot's account rows carry
   account_id + service_name.
2. The provisioning-issues queue — GET /api/shop/issues returns the orders a
   PAYING customer did not get their service from: stuck in `provisioning`
   past the sweeper timeout, and `failed` (money auto-refunded by design).

Run from backend/: venv/Scripts/python.exe tests/test_shop_service_naming_and_issues.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="shop_naming_issues_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""
os.environ["SHOP_BOT_API_KEY"] = "test-shop-key"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (Account, AccountEvent, Customer, ShopOrder, ShopOrderStatus,  # noqa: E402
                        ShopSettings, ShopUser, utcnow)
from app.shop_service import STUCK_ORDER_TIMEOUT_MINUTES  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


client = TestClient(app)
BOT = {"X-Shop-Bot-Key": "test-shop-key"}


def _auth_header() -> dict:
    from app.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token('test')}"}


AUTH = _auth_header()

with Session(engine) as session:
    for model in (ShopUser, ShopSettings):
        for row in session.exec(select(model)).all():
            session.delete(row)
    session.add(ShopSettings(id=1, is_open=True, price_per_gb=3000, min_gb=5, max_gb=200,
                             plan_duration_days=30, card_number="6037-XXXX", card_holder="Operator",
                             username_prefix="shop", min_topup=10000, max_topup=50_000_000))
    session.commit()

# Fixture: Ali (telegram 3131) owns a delivered order with account A1.
# A2 belongs to a linked customer (also Ali's, via the customer link).
# Sara (telegram 9999) owns nothing.
with Session(engine) as session:
    ali = ShopUser(telegram_id=3131, display_name="Ali")
    session.add(ali)
    session.commit()
    session.refresh(ali)
    cust = Customer(name="Ali Rezaei", shop_user_id=ali.id)
    session.add(cust)
    session.commit()
    session.refresh(cust)
    a1 = Account(marzban_username="shop1", customer_id=cust.id, status="active", data_limit=10 * 1024**3)
    session.add(a1)
    a2 = Account(marzban_username="ali_second", customer_id=cust.id, status="active")
    session.add(a2)
    session.commit()
    session.refresh(a1)
    session.refresh(a2)
    session.add(ShopOrder(shop_user_id=ali.id, data_limit_gb=10, duration_days=30, price=30_000,
                          status=ShopOrderStatus.delivered, marzban_username="shop1",
                          account_id=a1.id, created_at=utcnow()))
    session.commit()
    print(f"fixture: ali={ali.id} cust={cust.id} a1={a1.id} a2={a2.id}")
    ALI_A1, ALI_A2 = a1.id, a2.id

# ---- 1. rename happy path (a shop-order account) ----
r = client.post(f"/api/shop/bot/accounts/{ALI_A1}/name", headers=BOT,
                json={"telegram_id": 3131, "name": "گوشی علی"})
check("rename returns 200", r.status_code == 200)
check("new name echoed", r.json().get("service_name") == "گوشی علی")
with Session(engine) as session:
    a = session.get(Account, ALI_A1)
    check("persisted on the account", a.service_name == "گوشی علی")
    ev = session.exec(select(AccountEvent).where(
        AccountEvent.account_id == ALI_A1, AccountEvent.action == "service_renamed")).first()
    check("rename recorded in the audit trail", ev is not None)

# ---- 2. the bot's list carries the name + the account id ----
r = client.get("/api/shop/bot/accounts", headers=BOT, params={"telegram_id": 3131})
rows = r.json()
row_a1 = next((row for row in rows if row.get("account_id") == ALI_A1), None)
check("list row carries account_id", row_a1 is not None)
check("list row carries the nickname", row_a1 and row_a1.get("service_name") == "گوشی علی")

# ---- 3. rename a LINKED (non-shop-order) account — the customer link also counts ----
r = client.post(f"/api/shop/bot/accounts/{ALI_A2}/name", headers=BOT,
                json={"telegram_id": 3131, "name": "لپ‌تاپ"})
check("linked account rename ok", r.status_code == 200)

# ---- 4. ownership: another telegram cannot rename Ali's service ----
r = client.post(f"/api/shop/bot/accounts/{ALI_A1}/name", headers=BOT,
                json={"telegram_id": 9999, "name": "hacked"})
check("stranger rename -> 404 without an oracle", r.status_code == 404)
with Session(engine) as session:
    check("name unchanged after the stranger attempt",
          session.get(Account, ALI_A1).service_name == "گوشی علی")

# unknown account id -> same 404
r = client.post("/api/shop/bot/accounts/999999/name", headers=BOT,
                json={"telegram_id": 3131, "name": "x"})
check("unknown account -> 404", r.status_code == 404)

# ---- 5. empty name clears; >40 chars refused ----
r = client.post(f"/api/shop/bot/accounts/{ALI_A1}/name", headers=BOT,
                json={"telegram_id": 3131, "name": "  "})
check("whitespace name clears the nickname", r.status_code == 200 and r.json().get("service_name") is None)
r = client.post(f"/api/shop/bot/accounts/{ALI_A1}/name", headers=BOT,
                json={"telegram_id": 3131, "name": "ن" * 41})
check("41 characters refused", r.status_code == 400)
r = client.post(f"/api/shop/bot/accounts/{ALI_A1}/name", headers=BOT,
                json={"telegram_id": 3131, "name": "ن" * 40})
check("40 characters accepted", r.status_code == 200)

# ---- 6. the issues queue: a failed order and a stuck provisioning order ----
with Session(engine) as session:
    session.add(ShopOrder(shop_user_id=ali.id, data_limit_gb=30, duration_days=30, price=150_000,
                          status=ShopOrderStatus.failed,
                          error='Marzban create_user failed (400): Protocol ProxyTypes.VMess is disabled',
                          created_at=utcnow() - timedelta(minutes=20)))
    session.add(ShopOrder(shop_user_id=ali.id, data_limit_gb=5, duration_days=30, price=25_000,
                          status=ShopOrderStatus.provisioning,
                          created_at=utcnow() - timedelta(minutes=STUCK_ORDER_TIMEOUT_MINUTES + 30)))
    session.commit()
r = client.get("/api/shop/issues", headers=AUTH)
check("issues endpoint ok", r.status_code == 200)
d = r.json()
check("stuck section has the old provisioning order", d["counts"]["stuck"] == 1)
check("failed section has the failed order", d["counts"]["failed"] == 1)
check("failed row carries the Marzban error",
      "VMess" in (d["failed"][0]["error"] or ""))
check("failed rows name the customer", d["failed"][0]["customer"] == "Ali")
check("stuck rows carry the amount", d["stuck"][0]["price"] == 25_000)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("all service-naming and issues-queue cases passed.")
