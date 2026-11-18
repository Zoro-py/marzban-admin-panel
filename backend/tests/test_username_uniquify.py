"""Owner decision 2026-11-18: no more auto naming (the customer types their
own Marzban username), and a TAKEN name is uniquified with a numeric suffix
instead of bouncing the customer («یونیکش کن که مرزبان قبول کنه»).

  * _resolve_unique_username: base free → base; taken → first free -2..-50;
    a >30-char base is truncated to fit the suffix within Marzban's own rule;
    all 50 taken → the Persian error (rare).
  * POST /orders/{id}/username resolves taken names and reports was_taken,
    so the bot can tell the customer their final name.
  * Provisioning re-resolves (order creation and approval can be hours
    apart) — and adopts the FINAL name on the order.

Plain `python -m tests.test_username_uniquify` from `backend/`.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="uniq_user_test_")) / "test.db"
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
from sqlmodel import Session  # noqa: E402

from app.auth import require_auth  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ShopOrder, ShopOrderStatus, ShopUser  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class _Marzban:
    """Stub: `taken` are names the panel already knows."""

    taken: set[str] = set()

    @classmethod
    async def get_user(cls, username):
        return {"username": username} if username in cls.taken else None


import app.shop_service as shop_service  # noqa: E402
from app.routers import shop as shop_router  # noqa: E402

app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)
BOT = {"X-Shop-Bot-Key": "test-shop-key"}

UUID = "u-"


def _engine_of():
    from app.db import engine
    return engine


async def _resolve(name):
    return await shop_service._resolve_unique_username(name)


def main() -> None:
    shop_service.marzban_client = _Marzban

    # ── 1. the resolve helper ─────────────────────────────────────────────
    check("_resolve_unique_username exists",
          callable(getattr(shop_service, "_resolve_unique_username", None)))

    _Marzban.taken = set()
    check("a free base stays untouched", asyncio.run(_resolve("ali")) == "ali")

    _Marzban.taken = {"ali"}
    check("taken base → ali-2", asyncio.run(_resolve("ali")) == "ali-2")
    _Marzban.taken = {"ali", "ali-2", "ali-3"}
    check("two taken → ali-4", asyncio.run(_resolve("ali")) == "ali-4")

    long_base = "a" * 30
    _Marzban.taken = {long_base}
    got = asyncio.run(_resolve(long_base))
    check("a 30-char taken base is truncated to fit the suffix",
          got == "a" * 28 + "-2" and len(got) <= 30, repr(got))

    _Marzban.taken = {"zz"} | {f"zz-{n}" for n in range(2, 51)}
    try:
        asyncio.run(_resolve("zz"))
        raised = False
    except shop_service.ShopError:
        raised = True
    check("all nearby names taken → the Persian error, never a silent guess", raised)

    # ── 2. POST /orders/{id}/username resolves + reports ──────────────────
    with Session(_engine_of()) as session:
        user = ShopUser(telegram_id=555, display_name="t")
        session.add(user)
        session.commit()
        session.refresh(user)
        order = ShopOrder(shop_user_id=user.id, data_limit_gb=10, duration_days=30,
                          price=100_000, status=ShopOrderStatus.awaiting_payment)
        session.add(order)
        session.commit()
        oid = order.id

    _Marzban.taken = {"ali"}
    r = client.post(f"/api/shop/bot/orders/{oid}/username", headers=BOT,
                    json={"telegram_id": 555, "name": "ali"})
    check("taken name → 200 with the uniquified final", r.status_code == 200, repr(r.text))
    body = r.json()
    check("final name = ali-2", body.get("requested_username") == "ali-2", repr(body))
    check("was_taken reported", body.get("was_taken") is True, repr(body))
    with Session(_engine_of()) as session:
        check("the ORDER carries the final name",
              session.get(ShopOrder, oid).requested_username == "ali-2")

    r = client.post(f"/api/shop/bot/orders/{oid}/username", headers=BOT,
                    json={"telegram_id": 555, "name": "totally-free"})
    check("a free name → was_taken False", r.json().get("was_taken") is False, repr(r.text))

    r = client.post(f"/api/shop/bot/orders/{oid}/username", headers=BOT,
                    json={"telegram_id": 555, "name": "فارسی!"})
    check("invalid pattern still 400 with the Persian rule", r.status_code == 400, repr(r.text))


main()

print()
if failures:
    print(f"RESULT: {len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("RESULT: username-uniquify checks OK")
