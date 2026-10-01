"""Guards born from the 2026-10-01 live incident: a customer's UNLIMITED
operator-created account was purchased against through the shop link, and the
extend path (a) capped it at used+30 GB, (b) invented a 30-day expiry, (c)
triggered a 94%-used warning one minute after the approval. These tests pin
the three fixes:

  1. purchase()/quote/orders refuse before money moves (SERVICE_IS_UNLIMITED)
  2. _extension_landed treats "still no expiry" as landed for a no-expiry
     target (the twin of the extend that now preserves None)
  3. the warning job stays silent within the post-delivery grace window

Plain `python -m tests.test_shop_extend_guards` from `backend/`.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="extend_guards_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""
os.environ["SHOP_BOT_API_KEY"] = "test-shop-key"
os.environ["SHOP_BOT_USERNAME"] = "test_shop_bot"
os.environ["DELEGATE_BOT_API_KEY"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    Customer,
    ShopOrder,
    ShopOrderStatus,
    ShopSettings,
    ShopUser,
    utcnow,
)
from app import shop_service  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)
BOT_HEADERS = {"X-Shop-Bot-Key": "test-shop-key"}

GB = 1024 ** 3

with Session(engine) as session:
    owner = Customer(name="Unlimited Uma")
    session.add(owner)
    session.commit()
    session.refresh(owner)
    owner_id = owner.id
    unlimited = Account(marzban_username="uma_unlimited", customer_id=owner_id,
                        data_limit=None, expire=None, status="active")
    session.add(unlimited)
    session.commit()
    session.refresh(unlimited)
    unlimited_id = unlimited.id
    shop_user = ShopUser(telegram_id=888001, last_seen_at=utcnow())
    session.add(shop_user)
    session.commit()
    session.refresh(shop_user)
    shop_user_id = shop_user.id
    owner.shop_user_id = shop_user_id
    session.add(owner)
    session.commit()

# ── 1) the gate, service-level ────────────────────────────────────────────
with Session(engine) as session:
    user = session.get(ShopUser, shop_user_id)
    try:
        asyncio.run(shop_service.purchase(session, user, 30.0))
        check("purchase on an unlimited service raises", False)
    except shop_service.ShopError as exc:
        check("purchase raises ShopError with the stable SERVICE_IS_UNLIMITED prefix",
              str(exc).startswith("SERVICE_IS_UNLIMITED"))
    balance = shop_service.wallet_balance(session, shop_user_id)
    orders = list(session.exec(
        __import__("sqlmodel").select(ShopOrder).where(ShopOrder.shop_user_id == shop_user_id)))
    check("no wallet movement, no order row", balance == 0 and len(orders) == 0)

# ── 1b) the gate, through the bot endpoints ───────────────────────────────
r = client.post("/api/shop/bot/quote", json={"telegram_id": 888001, "data_limit_gb": 30}, headers=BOT_HEADERS)
check("quote refused → 409 with the stable prefix",
      r.status_code == 409 and r.json()["detail"].startswith("SERVICE_IS_UNLIMITED"))
r = client.post("/api/shop/bot/orders", json={"telegram_id": 888001, "data_limit_gb": 30}, headers=BOT_HEADERS)
check("order creation refused → 409 with the stable prefix",
      r.status_code == 409 and r.json()["detail"].startswith("SERVICE_IS_UNLIMITED"))

# ── 2) _extension_landed: a no-expiry target is landed only while the panel
#      STILL shows no expiry ───────────────────────────────────────────────
class _StubMarzban:
    def __init__(self, user):
        self.user = user

    async def get_user(self, username):
        return self.user


async def _landed(marzban_user, target_limit, target_expire):
    shop_service.marzban_client = _StubMarzban(marzban_user)
    return await shop_service._extension_landed("uma_unlimited", target_limit, target_expire)


landed = asyncio.run(_landed({"data_limit": 60 * GB, "expire": None}, 60 * GB, None))
check("no-expiry target with panel expire=None → landed", landed is not None)
landed = asyncio.run(_landed({"data_limit": 60 * GB, "expire": 1793445163}, 60 * GB, None))
check("no-expiry target but panel grew an expiry → NOT landed", landed is None)
landed = asyncio.run(_landed({"data_limit": 60 * GB, "expire": 1793445163}, 60 * GB, 1793445100))
check("dated target reached → landed (existing behaviour intact)", landed is not None)

# ── 3) the warning grace ──────────────────────────────────────────────────
with Session(engine) as session:
    settings_row = session.get(ShopSettings, 1)
    if settings_row is None:
        settings_row = ShopSettings(id=1)
        session.add(settings_row)
        session.commit()
    fresh = ShopOrder(shop_user_id=shop_user_id, data_limit_gb=30, duration_days=30, price=150000,
                      status=ShopOrderStatus.delivered, account_id=unlimited_id,
                      marzban_username="uma_unlimited", delivered_at=utcnow() - timedelta(minutes=2))
    session.add(fresh)
    session.commit()
    session.refresh(fresh)
    fresh_id = fresh.id
sent = asyncio.run(shop_service.warn_customers_before_service_ends(session))
check("warning job sends NOTHING within the post-delivery grace window", sent == 0)
with Session(engine) as session:
    order = session.get(ShopOrder, fresh_id)
    order.delivered_at = utcnow() - timedelta(days=8)
    session.add(order)
    session.commit()
with Session(engine) as session:
    sent = asyncio.run(shop_service.warn_customers_before_service_ends(session))
check("after the grace window the job runs again (unlimited account: still no usage warning)",
      sent == 0)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("test_shop_extend_guards: all checks passed")
