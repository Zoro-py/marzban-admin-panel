"""The panel-selected shop inbounds (ShopSettings.shop_inbound_tags) are the
AUTHORITATIVE source for what goes into shop-created accounts — owner rule
2026-10-23: shop accounts are vless-only, no shadowsocks, and the operator
picks the tags in the panel from the server's LIVE inbounds.

Locked here: PATCH persists + validates against the live list (unknown tag →
400), the create payload derives BOTH proxies and per-host inbounds from the
selection (no shadowsocks anywhere when it isn't selected), and clearing the
selection falls back to omitting the keys (Marzban panel defaults).

Run from backend/: venv/Scripts/python.exe tests/test_shop_inbound_selection.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="shop_inbound_select_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""
os.environ["SHOP_BOT_API_KEY"] = "test-shop-key"
os.environ["SHOP_BOT_USERNAME"] = "testshopbot"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import marzban_client as marzban_module  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


LIVE_INBOUNDS = {
    "v1.zoropy.ir": [
        {"tag": "VLESS_XHTTP_Unified", "protocol": "vless"},
        {"tag": "VLESS_TCP_TLS_6001", "protocol": "vless"},
        {"tag": "Shadowsocks TCP", "protocol": "shadowsocks"},
    ]
}

created_payloads: list[dict] = []
modified_payloads: list[tuple] = []
list_calls = 0


async def fake_create_user(payload: dict) -> dict:
    created_payloads.append(payload)
    return {"username": payload["username"], "inbounds": {}, "data_limit": payload.get("data_limit"),
            "expire": payload.get("expire"), "status": "active", "used_traffic": 0,
            "subscription_url": "/sub/tok"}


async def fake_list_inbounds() -> dict:
    global list_calls
    list_calls += 1
    return LIVE_INBOUNDS


async def fake_get_user(username: str) -> dict:
    return {"username": username, "data_limit": 5 * 1024 ** 3, "expire": 4102444800,
            "used_traffic": 0, "status": "active", "inbounds": {}}


async def fake_modify_user(username: str, payload: dict) -> dict:
    modified_payloads.append((username, payload))
    return {"username": username, **payload}


created_payloads_will_extend = fake_get_user  # naming clarity only
marzban_module.marzban_client.create_user = fake_create_user
marzban_module.marzban_client.list_inbounds = fake_list_inbounds
marzban_module.marzban_client.get_user = fake_get_user
marzban_module.marzban_client.modify_user = fake_modify_user

client = TestClient(app)
AUTH = {"Authorization": "Bearer test-token-placeholder"}


def _auth_header() -> dict:
    from app.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token('test')}"}


AUTH = _auth_header()
BOT = {"X-Shop-Bot-Key": "test-shop-key"}


def reset_db() -> None:
    from app.models import ShopSettings, ShopUser

    with Session(engine) as session:
        for model in (ShopUser, ShopSettings):
            for row in session.exec(select(model)).all():
                session.delete(row)
        session.add(ShopSettings(id=1, is_open=True, price_per_gb=3000, min_gb=5, max_gb=200,
                                 plan_duration_days=30, card_number="6037-XXXX", card_holder="Operator",
                                 username_prefix="shop", min_topup=10000, max_topup=50_000_000))
        session.commit()


reset_db()

# ---- 1. GET /inbounds returns the LIVE list with selection state ----
r = client.get("/api/shop/inbounds", headers=AUTH)
check("GET /inbounds ok", r.status_code == 200)
d = r.json()
tags = {i["tag"] for i in d["inbounds"]}
check("live list carries all three tags", tags == {"VLESS_XHTTP_Unified", "VLESS_TCP_TLS_6001", "Shadowsocks TCP"})
check("protocols attached", {i["protocol"] for i in d["inbounds"] if i["protocol"] == "shadowsocks"} == {"shadowsocks"})
check("nothing selected yet", d["selected"] == [])

# ---- 2. unknown tag refused against the LIVE list ----
r = client.patch("/api/shop/settings", headers=AUTH,
                 json={"shop_inbound_tags": ["VLESS_XHTTP_Unified", "GHOST_TAG"]})
check("unknown tag refused with 400", r.status_code == 400)
check("refusal names the ghost tag", "GHOST_TAG" in r.json().get("detail", ""))

# ---- 3. vless-only selection persists (round-trips as a parsed list) ----
r = client.patch("/api/shop/settings", headers=AUTH,
                 json={"shop_inbound_tags": ["VLESS_XHTTP_Unified", "VLESS_TCP_TLS_6001"]})
check("vless-only selection saves", r.status_code == 200)
check("selection round-trips parsed", r.json().get("shop_inbound_tags") == ["VLESS_TCP_TLS_6001", "VLESS_XHTTP_Unified"])

# ---- 4. a purchase builds the payload FROM the selection: vless proxies +
# per-host vless inbounds, shadowsocks NOWHERE ----
client.post("/api/shop/bot/session", headers=BOT,
            json={"telegram_id": 3131, "display_name": "Inbound Test"})
with Session(engine) as session:
    from app.models import ShopUser, ShopWalletEntry, ShopWalletEntryType, utcnow

    uid = session.exec(select(ShopUser)).first().id
    session.add(ShopWalletEntry(shop_user_id=uid, type=ShopWalletEntryType.topup,
                                amount=500_000, note="test credit", created_at=utcnow()))
    session.commit()
r = client.post("/api/shop/bot/purchase", headers=BOT,
                json={"telegram_id": 3131, "data_limit_gb": 5})
check("purchase succeeds", r.status_code == 200)
check("one create captured", len(created_payloads) == 1)
payload = created_payloads[0] if created_payloads else {}
check("proxies derived from the selection (vless only)",
      payload.get("proxies") == {"vless": {}})
check("inbounds per-host from the selection",
      payload.get("inbounds") == {"v1.zoropy.ir": ["VLESS_XHTTP_Unified", "VLESS_TCP_TLS_6001"]})
check("shadowsocks appears NOWHERE in the payload",
      "shadowsocks" not in json.dumps(payload).lower())

# ---- 5. clearing the selection falls back to omitting the keys ----
r = client.patch("/api/shop/settings", headers=AUTH, json={"shop_inbound_tags": []})
check("empty selection clears", r.status_code == 200 and r.json().get("shop_inbound_tags") in (None, []))
oid = client.post("/api/shop/bot/orders", headers=BOT,
                  json={"telegram_id": 3131, "data_limit_gb": 5, "force_new_service": True}
                  ).json()["order_id"]
r = client.post(f"/api/shop/bot/orders/{oid}/pay", headers=BOT, json={"telegram_id": 3131})
check("second purchase (force-new, order-first) succeeds", r.status_code == 200)
check("a second create was captured", len(created_payloads) == 2)
payload2 = created_payloads[-1] if created_payloads else {}
check("cleared selection omits both keys (Marzban defaults)",
      "proxies" not in payload2 and "inbounds" not in payload2)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("all shop inbound-selection cases passed.")
