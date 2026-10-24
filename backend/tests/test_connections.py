"""Creation origin (created_via) + the telegram-connections endpoints.

Locks the admin-UX rebuild's two data additions (2026-10-24):

  1. `created_via` on every AccountRow — the operator's «کدوم خودم زدم کدوم
     باته» question, derived in enrich_accounts from (in order): a delivered
     ShopOrder (shop), the account's newest create/delegate_create AccountEvent
     (web/bulk/delegate), else sync adoption.
  2. GET /api/connections — one row per customer with the shop-bot link state
     (linked/pending/none) and the collapsed delegate grant
     (claimed/pending/revoked) side by side.
  3. GET /api/connections/{id}/bot-history — the merged per-customer bot
     timeline assembled from rows other flows already wrote (orders, topups,
     wallet entries, bot/delegate AccountEvents + LedgerEntries), newest
     first, voided ledger rows excluded.

Plain `python -m tests.test_connections` from `backend/` — no pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="conn_test_")) / "test.db"
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
from sqlmodel import Session  # noqa: E402

from app import marzban_client as marzban_module  # noqa: E402
from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account, AccountEvent, Customer, Delegate, LedgerEntry, LedgerSource,
    ShopOrder, ShopOrderStatus, ShopTopup, ShopTopupStatus, ShopUser,
    ShopWalletEntry, ShopWalletEntryType,
)

init_db()

failures: list[str] = []


def check(name: str, cond: bool, extra=None):
    print(("  [OK] " if cond else "[FAIL] ") + name + (f"  {extra}" if (extra is not None and not cond) else ""))
    if not cond:
        failures.append(name)


# The panel's create endpoint round-trips Marzban — fake it (same pattern as
# tests/test_created_by.py: instance attributes, no self binding).
async def _fake_create_user(payload: dict) -> dict:
    return {"username": payload.get("username"), "used_traffic": 0, "lifetime_used_traffic": 0,
            "data_limit": payload.get("data_limit"), "expire": payload.get("expire"),
            "status": "active", "subscription_url": "https://sub.test/u/x"}


async def _fake_delete_user(username: str) -> None:
    return None


marzban_module.marzban_client.create_user = _fake_create_user
marzban_module.marzban_client.delete_user = _fake_delete_user

app.dependency_overrides[require_auth] = lambda: "testop"
client = TestClient(app)

now = datetime.utcnow().replace(tzinfo=None)

with Session(engine) as s:
    # ── customers ────────────────────────────────────────────────────────
    c_op = Customer(name="Op Customer")           # web-created account
    c_shop = Customer(name="Shop Customer")       # shop-linked, bot-bought
    c_del = Customer(name="Delegate Customer")    # delegate-created account
    c_sync = Customer(name="Sync Customer")       # adopted by sync (no event)
    c_none = Customer(name="Bare Customer")       # nothing at all
    for c in (c_op, c_shop, c_del, c_sync, c_none):
        s.add(c)
    s.flush()

    # ── accounts, one per origin ─────────────────────────────────────────
    a_web = Account(marzban_username="webuser", customer_id=c_op.id)
    a_shop = Account(marzban_username="shopuser7", customer_id=c_shop.id)
    a_del = Account(marzban_username="d3user", customer_id=c_del.id)
    a_sync = Account(marzban_username="syncuser", customer_id=c_sync.id)
    for a in (a_web, a_shop, a_del, a_sync):
        s.add(a)
    s.flush()

    s.add(AccountEvent(account_id=a_web.id, action="create", detail="Created via dashboard", created_by="admin"))
    s.add(AccountEvent(account_id=a_del.id, action="delegate_create",
                       detail="data_limit_gb=10, duration_days=30, delegate_id=1",
                       source=LedgerSource.delegate))
    order = ShopOrder(shop_user_id=0, data_limit_gb=10, duration_days=30, price=50000,
                      status=ShopOrderStatus.delivered, account_id=a_shop.id,
                      marzban_username="shopuser7", service_name="گوشی علی")
    s.add(order)
    s.flush()
    # the shop's create event (as shop_service writes it — detail identifies
    # the order; the ShopOrder join is the authoritative origin signal)
    s.add(AccountEvent(account_id=a_shop.id, action="create", detail=f"Sold via shop order #{order.id}"))
    # a_sync gets NO event — the sync job mints rows silently
    s.commit()

    op_web_id, op_shop_id, op_del_id, op_sync_id, op_none_id = c_op.id, c_shop.id, c_del.id, c_sync.id, c_none.id
    shop_user = ShopUser(telegram_id=111222333, telegram_username="ali_tg", display_name="علی",
                         customer_id=c_shop.id, last_seen_at=now)
    s.add(shop_user)
    s.flush()
    c_shop.shop_user_id = shop_user.id
    c_shop.shop_link_linked_at = now - timedelta(days=3)
    s.add(c_shop)

    order.shop_user_id = shop_user.id
    s.add(order)
    s.add(ShopWalletEntry(shop_user_id=shop_user.id, type=ShopWalletEntryType.topup, amount=50000, note="card 6060"))
    s.add(ShopTopup(shop_user_id=shop_user.id, claimed_amount=50000, approved_amount=50000,
                    status=ShopTopupStatus.approved, reference_code="A7K2", order_id=order.id,
                    created_at=now - timedelta(hours=2)))
    s.add(LedgerEntry(type="charge", amount=120000.0, account_id=a_del.id, source=LedgerSource.delegate,
                      note="Delegate self-service: created 10GB / 30d", gb_amount=10.0))

    d_active = Delegate(customer_id=c_del.id, telegram_id=999888777, label="نماینده علی")
    d_pending_for_op = Delegate(customer_id=c_op.id, claim_token="tok123", claim_expires_at=now + timedelta(days=7))
    s.add(d_active)
    s.add(d_pending_for_op)
    s.commit()
    DEL_ID = d_active.id

# ---- 1) created_via on AccountRow -----------------------------------------
rows = {r["marzban_username"]: r for r in client.get("/api/accounts").json()}
check("origin: web → created_via=web + operator", rows["webuser"]["created_via"] == "web" and rows["webuser"]["created_via_detail"] == "admin", rows["webuser"].get("created_via"))
check("origin: shop order wins → created_via=shop + order ref", rows["shopuser7"]["created_via"] == "shop" and "order #" in (rows["shopuser7"]["created_via_detail"] or ""), rows["shopuser7"].get("created_via"))
check("origin: delegate event → created_via=delegate", rows["d3user"]["created_via"] == "delegate", rows["d3user"].get("created_via"))
check("origin: no event → created_via=sync (adopted)", rows["syncuser"]["created_via"] == "sync", rows["syncuser"].get("created_via"))

# the panel's own create path stamps origin=web end-to-end (POST returns the
# plain Account shape; the enriched row comes from the list)
r = client.post("/api/accounts", json={"marzban_username": "fresh_web_user"})
fresh_id = r.json()["id"] if r.status_code == 200 else None
fresh = next((a for a in client.get("/api/accounts").json() if a["id"] == fresh_id), None)
check("origin: POST /api/accounts → created_via=web", fresh is not None and fresh["created_via"] == "web", fresh)
client.post(f"/api/accounts/{fresh_id}/delete")

# ---- 2) /api/connections ----------------------------------------------------
conns = {c["customer_id"]: c for c in client.get("/api/connections").json()}
check("connections: one row per customer", len(conns) >= 5, len(conns))
shop_row = conns[op_shop_id]
check("connections: shop linked state", shop_row["shop"]["state"] == "linked", shop_row["shop"])
check("connections: linked carries telegram identity", shop_row["shop"]["telegram_id"] == 111222333 and shop_row["shop"]["display_name"] == "علی")
check("connections: linked carries wallet balance", shop_row["shop"]["wallet_balance"] == 50000, shop_row["shop"]["wallet_balance"])
check("connections: linked_at present", shop_row["shop"]["linked_at"] is not None)
check("connections: delegate claimed for delegate customer", conns[op_del_id]["delegate"]["state"] == "claimed", conns[op_del_id]["delegate"])
check("connections: pending invite for op customer", conns[op_op_id := op_web_id]["delegate"]["state"] == "pending", conns[op_web_id]["delegate"])
check("connections: bare customer → none/none", conns[op_none_id]["shop"]["state"] == "none" and conns[op_none_id]["delegate"] is None)

# ---- 3) bot-history --------------------------------------------------------
hist = client.get(f"/api/connections/{op_shop_id}/bot-history").json()
kinds = [h["kind"] for h in hist]
check("bot-history: order + topup + wallet all present", {"order", "topup", "wallet"} <= set(kinds), kinds)
order_item = next(h for h in hist if h["kind"] == "order")
check("bot-history: order title delivered", "تحویل" in order_item["title"], order_item["title"])
check("bot-history: order detail carries service name", "گوشی علی" in (order_item["detail"] or ""), order_item["detail"])
topup_item = next(h for h in hist if h["kind"] == "topup")
check("bot-history: topup approved title + amount", topup_item["amount"] == 50000.0 and "تایید" in topup_item["title"], topup_item)
check("bot-history: newest first", hist == sorted(hist, key=lambda h: h["ts"], reverse=True))

del_hist = client.get(f"/api/connections/{op_del_id}/bot-history").json()
check("bot-history: delegate ledger charge present", any(h["kind"] == "ledger" and h["amount"] == 120000.0 for h in del_hist), del_hist)
check("bot-history: delegate account_event present", any(h["kind"] == "account_event" and "نماینده" in h["title"] for h in del_hist), [h["title"] for h in del_hist])

r404 = client.get("/api/connections/999999/bot-history")
check("bot-history: unknown customer → 404", r404.status_code == 404, r404.status_code)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All connections/origin cases passed.")
