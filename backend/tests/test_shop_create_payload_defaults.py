"""The shop's Marzban create payload must OMIT proxies/inbounds when the
operator hasn't configured them (config default {}), so Marzban applies its
own panel defaults — the only contract that works on every server. Sending a
hardcoded protocol list 400-rejected every shop-created user on servers
without that protocol (live 2026-10-23: "Protocol ProxyTypes.VMess is
disabled on your server" — a paying customer's wallet was credited fine and
three orders failed in a row).

Run from backend/: venv/Scripts/python.exe tests/test_shop_create_payload_defaults.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="shop_create_payload_test_")) / "test.db"
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
from app.config import settings  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


created_payloads: list[dict] = []


async def fake_create_user(payload: dict) -> dict:
    created_payloads.append(payload)
    return {"username": payload["username"], "inbounds": {}}


# Patch the singleton's METHODS (shop_service holds a direct reference to the
# instance, so replacing the module attribute would not propagate — the same
# approach tests/test_shop.py uses).
marzban_module.marzban_client.create_user = fake_create_user
# The shopbot bot-side delivery push reads SHOP_BOT_TOKEN and would raise; the
# provision path's delivery is best-effort — importing app with the token
# unset already covers it.

client = TestClient(app)

# Guard: this test only means something with the config default EMPTY (env
# unset, code default {}) — the omission is the whole contract under test.
check("config default proxies is empty (env unset)", settings.marzban_default_proxies == {})
check("config default inbounds is empty (env unset)", settings.marzban_default_inbounds == {})

# Fund a wallet + buy, through the normal bot flow.
headers = {"X-Shop-Bot-Key": "test-shop-key"}
with Session(engine) as session:
    from app.models import ShopSettings, ShopUser

    for row in session.exec(select(ShopSettings)).all():
        session.delete(row)
    for row in session.exec(select(ShopUser)).all():
        session.delete(row)
    session.add(ShopSettings(id=1, is_open=True, price_per_gb=3000, min_gb=5, max_gb=200,
                             plan_duration_days=30, card_number="6037-XXXX", card_holder="Operator",
                             username_prefix="shop", min_topup=10000, max_topup=50_000_000))
    session.commit()

client.post("/api/shop/bot/session", headers=headers,
            json={"telegram_id": 4242, "display_name": "Payload Test"})
with Session(engine) as session:
    from app.models import ShopWalletEntry, ShopWalletEntryType, utcnow

    uid = session.exec(select(ShopUser)).first().id
    session.add(ShopWalletEntry(shop_user_id=uid, type=ShopWalletEntryType.topup,
                                amount=500_000, note="test credit", created_at=utcnow()))
    session.commit()

r = client.post("/api/shop/bot/purchase", headers=headers,
                json={"telegram_id": 4242, "data_limit_gb": 5})
check("purchase with funded wallet succeeds", r.status_code == 200)
check("exactly one Marzban create captured", len(created_payloads) == 1)

payload = created_payloads[0] if created_payloads else {}
check("payload OMITS proxies when config is empty", "proxies" not in payload)
check("payload OMITS inbounds when config is empty", "inbounds" not in payload)
check("payload still carries the essentials",
      all(k in payload for k in ("username", "expire", "data_limit", "data_limit_reset_strategy", "status")))

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("all shop create-payload default cases passed.")
