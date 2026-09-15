"""End-to-end check: does the operator alert actually carry the typed
receipt text and the dupe-reuse warning, built by _alert_operator_to_topup
in app/routers/shop.py?

Plain `python -m tests.test_receipt_alert` from `backend/`, same harness
shape as tests/test_shop.py (TestClient + FakeMarzban), because the caption
string is built inside a BackgroundTask this test needs to actually run.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="receipt_alert_test_")) / "test.db"
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

from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, AccountEvent, ShopOrder, ShopSettings, ShopTopup, ShopUser, ShopWalletEntry  # noqa: E402

init_db()

BOT_HEADERS = {"X-Shop-Bot-Key": "test-shop-key"}
failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


def _reset() -> TestClient:
    with Session(engine) as session:
        for model in (ShopWalletEntry, ShopTopup, ShopOrder, ShopUser, AccountEvent, Account, ShopSettings):
            for row in session.exec(select(model)).all():
                session.delete(row)
        session.commit()
        session.add(ShopSettings(
            id=1, is_open=True, price_per_gb=3000, min_gb=5, max_gb=200,
            plan_duration_days=30, card_number="6037-XXXX", card_holder="Operator",
            username_prefix="shop", min_topup=10000, max_topup=50_000_000,
        ))
        session.commit()
    app.dependency_overrides[require_auth] = lambda: "test-admin"
    return TestClient(app)


client = _reset()
client.post("/api/shop/bot/session", headers=BOT_HEADERS, json={"telegram_id": 555, "display_name": "Ali"})
client.post("/api/shop/bot/session", headers=BOT_HEADERS, json={"telegram_id": 556, "display_name": "Reza"})

from app.routers import shop as shop_router  # noqa: E402

sent: list[tuple[str, dict]] = []
original = shop_router.notify_admin_with_buttons


async def capture(text, reply_markup=None):
    sent.append((text, reply_markup))


shop_router.notify_admin_with_buttons = capture
try:
    # First topup with a typed receipt: no prior use, no dupe warning.
    r1 = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                     json={"telegram_id": 555, "claimed_amount": 30_000, "receipt_text": "TRX-778899"})
    check("first topup with receipt_text created", r1.status_code == 200)
    check("exactly one alert sent so far", len(sent) == 1)
    caption1 = sent[0][0]
    check("caption shows the typed receipt text", "TRX-778899" in caption1)
    check("caption has no dupe warning on first use", "already used" not in caption1)

    # Second topup, different customer, SAME receipt text -> dupe warning.
    r2 = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                     json={"telegram_id": 556, "claimed_amount": 25_000, "receipt_text": "trx-778899"})
    check("second topup (case-different) also created", r2.status_code == 200)
    check("a second alert was sent", len(sent) == 2)
    caption2 = sent[1][0]
    check("dupe warning references the first topup's id", f"#{r1.json()['id']}" in caption2)
    check("dupe warning text present", "already used" in caption2)
    check("second caption also shows its own typed text", "trx-778899" in caption2)

    # A photo-only topup (no receipt_text) shows neither line.
    r3 = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                     json={"telegram_id": 555, "claimed_amount": 15_000, "receipt_file_id": "file123"})
    check("photo-only topup created", r3.status_code == 200)
    caption3 = sent[2][0]
    check("photo-only alert has no typed-receipt line", "Typed receipt" not in caption3)
    check("photo-only alert has no dupe line", "already used" not in caption3)
finally:
    shop_router.notify_admin_with_buttons = original

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All receipt-alert composition cases passed.")
