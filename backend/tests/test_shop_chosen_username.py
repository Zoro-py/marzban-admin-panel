"""Backend test: the customer-chosen Marzban username end-to-end.

Owner rule 2026-10-24: a shop customer picks their OWN subscription username
(the name their VPN app shows), instead of the auto shop{id}. Availability is
checked BEFORE money moves, the choice rides the order, and provisioning uses
it. Extensions and unnamed purchases keep the auto name.
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="shop_username_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
for k, v in {"MARZBAN_BASE_URL": "https://panel.test", "MARZBAN_USERNAME": "t", "MARZBAN_PASSWORD": "t",
             "BOT_TOKEN": "", "BOT_ADMIN_CHAT_ID": "", "SHOP_BOT_TOKEN": "",
             "SHOP_BOT_API_KEY": "test-shop-key"}.items():
    os.environ[k] = v
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import marzban_client as marzban_module  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


created: list[dict] = []
panel_users: dict = {}


async def fake_create_user(payload: dict) -> dict:
    created.append(payload)
    if payload["username"] in panel_users:
        raise ValueError("user already exists")
    panel_users[payload["username"]] = dict(payload)
    return {"username": payload["username"], "inbounds": {}, "data_limit": payload.get("data_limit"),
            "expire": payload.get("expire"), "status": "active", "used_traffic": 0,
            "subscription_url": "/sub/tok"}


async def fake_get_user(username: str):
    return panel_users.get(username)


async def fake_modify_user(username: str, payload: dict) -> dict:
    panel_users.setdefault(username, {}).update(payload)
    return {"username": username}


marzban_module.marzban_client.create_user = fake_create_user
marzban_module.marzban_client.get_user = fake_get_user
marzban_module.marzban_client.modify_user = fake_modify_user

client = TestClient(app)
BOT = {"X-Shop-Bot-Key": "test-shop-key"}

from app.models import (Account, ShopOrder, ShopSettings, ShopUser,  # noqa: E402
                        ShopWalletEntry, ShopWalletEntryType, utcnow)

with Session(engine) as session:
    session.add(ShopSettings(id=1, is_open=True, price_per_gb=3000, min_gb=5, max_gb=200,
                             plan_duration_days=30, card_number="c", card_holder="o",
                             username_prefix="shop", min_topup=10000, max_topup=50_000_000))
    session.commit()

client.post("/api/shop/bot/session", headers=BOT, json={"telegram_id": 777, "display_name": "U"})
with Session(engine) as session:
    uid = session.exec(select(ShopUser)).first().id
    session.add(ShopWalletEntry(shop_user_id=uid, type=ShopWalletEntryType.topup,
                                amount=500_000, note="t", created_at=utcnow()))
    session.commit()

# 1. the intent flags creates_new for a customer with no service
r = client.post("/api/shop/bot/orders", headers=BOT,
                json={"telegram_id": 777, "data_limit_gb": 5})
check("order ok", r.status_code == 200)
check("intent flags creates_new", r.json().get("creates_new") is True)

# 2. a TAKEN username is refused BEFORE the money — no new order row
with Session(engine) as session:
    before = len(session.exec(select(ShopOrder)).all())
panel_users["taken_name"] = {"username": "taken_name"}
r = client.post("/api/shop/bot/orders", headers=BOT,
                json={"telegram_id": 777, "data_limit_gb": 5, "marzban_username": "taken_name"})
check("taken username -> 409", r.status_code == 409)
check("refusal is the Persian sentence", "قبلاً گرفته شده" in r.json().get("detail", ""))
with Session(engine) as session:
    check("no order row was created for the refused name",
          len(session.exec(select(ShopOrder)).all()) == before)

# 3. an invalid pattern is refused with 400 (the Persian sentence)
r = client.post("/api/shop/bot/orders", headers=BOT,
                json={"telegram_id": 777, "data_limit_gb": 5, "marzban_username": "نام فارسی!"})
check("invalid pattern -> 400", r.status_code == 400)

# 4. a free username is stored on the order, and provisioning USES it
oid1 = client.post("/api/shop/bot/orders", headers=BOT,
                   json={"telegram_id": 777, "data_limit_gb": 5, "marzban_username": "fateme"}
                   ).json()["order_id"]
with Session(engine) as session:
    check("requested_username stored on the order",
          session.get(ShopOrder, oid1).requested_username == "fateme")
r = client.post(f"/api/shop/bot/orders/{oid1}/pay", headers=BOT, json={"telegram_id": 777})
check("pay succeeds", r.status_code == 200)
check("provisioning used the chosen username",
      created and created[-1]["username"] == "fateme")

# 5. the per-order username endpoint works on a STILL-AWAITING order
oid2 = client.post("/api/shop/bot/orders", headers=BOT,
                   json={"telegram_id": 777, "data_limit_gb": 10}).json()["order_id"]
r = client.post(f"/api/shop/bot/orders/{oid2}/username", headers=BOT,
                json={"telegram_id": 777, "name": "fateme2"})
check("username endpoint ok", r.status_code == 200)
with Session(engine) as session:
    check("order re-stored", session.get(ShopOrder, oid2).requested_username == "fateme2")
r = client.post(f"/api/shop/bot/orders/{oid2}/username", headers=BOT,
                json={"telegram_id": 9999, "name": "hax"})
check("stranger -> 404", r.status_code == 404)
r = client.post(f"/api/shop/bot/orders/{oid2}/username", headers=BOT,
                json={"telegram_id": 777, "name": "taken_name"})
check("endpoint also refuses a taken name", r.status_code == 409)
r = client.post(f"/api/shop/bot/orders/{oid2}/username", headers=BOT,
                json={"telegram_id": 777, "name": ""})
check("empty name clears the choice", r.status_code == 200
      and r.json().get("requested_username") is None)

# 6. an UNNAMED force-new purchase keeps the auto shop{id} name (the bot's
# force-new flow is order-first — the wallet /purchase path never carries
# force_new; that is the bot's established behavior, not a regression).
oid3 = client.post("/api/shop/bot/orders", headers=BOT,
                   json={"telegram_id": 777, "data_limit_gb": 5, "force_new_service": True}
                   ).json()["order_id"]
r = client.post(f"/api/shop/bot/orders/{oid3}/pay", headers=BOT, json={"telegram_id": 777})
check("unnamed force-new purchase ok", r.status_code == 200)
check("auto username = shop{id}", created[-1]["username"].startswith("shop"))

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("all chosen-username cases passed.")
