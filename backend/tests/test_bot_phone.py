"""Checks for POST /api/shop/bot/phone (opt-in phone capture).

Plain `python -m tests.test_bot_phone` from `backend/`, same harness shape
as tests/test_shop.py.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="bot_phone_test_")) / "test.db"
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

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()

client = TestClient(app)
BOT_HEADERS = {"X-Shop-Bot-Key": "test-shop-key"}

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


# Create the shop user first, as the bot always does via /session.
r = client.post("/api/shop/bot/session", headers=BOT_HEADERS, json={"telegram_id": 555})
check("session creates the shop user", r.status_code == 200)
check("phone starts unset", r.json().get("phone") is None)

# Unknown telegram_id -> 404 (bot never calls this before /session).
r = client.post("/api/shop/bot/phone", headers=BOT_HEADERS, json={"telegram_id": 999999, "phone": "09120000000"})
check("unknown user returns 404", r.status_code == 404)

# Known user -> saved.
r = client.post("/api/shop/bot/phone", headers=BOT_HEADERS, json={"telegram_id": 555, "phone": "09121234567"})
check("known user phone saved", r.status_code == 200 and r.json().get("ok") is True)

# Session now reflects the saved phone.
r = client.post("/api/shop/bot/session", headers=BOT_HEADERS, json={"telegram_id": 555})
check("session now returns the saved phone", r.json().get("phone") == "09121234567")

# Wrong / missing bot key is rejected (auth boundary, same as every other
# bot_router endpoint).
r = client.post("/api/shop/bot/phone", headers={"X-Shop-Bot-Key": "wrong"}, json={"telegram_id": 555, "phone": "0912"})
check("wrong bot key rejected", r.status_code in (401, 403))

# Overlong phone rejected by schema validation (max_length=32).
r = client.post("/api/shop/bot/phone", headers=BOT_HEADERS, json={"telegram_id": 555, "phone": "0" * 40})
check("overlong phone rejected (422)", r.status_code == 422)

# Too-short phone rejected by schema validation (min_length=5).
r = client.post("/api/shop/bot/phone", headers=BOT_HEADERS, json={"telegram_id": 555, "phone": "12"})
check("too-short phone rejected (422)", r.status_code == 422)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All bot phone-capture cases passed.")
