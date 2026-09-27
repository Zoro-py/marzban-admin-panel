"""C12 heartbeat (2026-09-27 audit): the operator's Telegram notification
channel is the one channel every automatic job reports through, so when
Telegram itself breaks, nothing else can raise the alarm. notify.py now
stamps a file on every OPERATOR-channel success, and /api/reports/summary
exposes when that last delivery happened — a stale stamp is the visible
symptom instead of silence.

Plain `python -m tests.test_notify_heartbeat` from `backend/` — no pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="heartbeat_test_")) / "test.db"
_STAMP = Path(tempfile.mkdtemp(prefix="heartbeat_test_")) / "notify_heartbeat.txt"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ["NOTIFY_HEARTBEAT_FILE"] = str(_STAMP)
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = "123:test"
os.environ["BOT_ADMIN_CHAT_ID"] = "1"
os.environ["SHOP_BOT_TOKEN"] = ""
os.environ["SHOP_BOT_API_KEY"] = ""
os.environ["DELEGATE_BOT_API_KEY"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.auth import require_auth  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402
from app import notify as notify_mod  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class _StubResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = "stub"


class _StubClient:
    """Replaces httpx.AsyncClient inside notify.py — records the send and
    replies with a fixed status, so no network is ever touched."""

    last_status = 200

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _StubResponse(_StubClient.last_status)


notify_mod.httpx.AsyncClient = _StubClient

app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)


async def _send():
    await notify_mod.notify_admin("test message")


import asyncio  # noqa: E402

asyncio.run(_send())
check("a successful operator send writes the stamp file", _STAMP.exists())
first = notify_mod.last_notify_success()
check("the stamp parses as a recent UTC timestamp",
      first is not None and abs((datetime.now(timezone.utc) - first).total_seconds()) < 30)

_StubClient.last_status = 500
try:
    asyncio.run(_send())
    raised = False
except RuntimeError:
    raised = True
check("a rejected send raises (the notify-first contract is untouched)", raised)
second = notify_mod.last_notify_success()
check("a FAILED send does NOT refresh the stamp", second == first)

_StubClient.last_status = 200
asyncio.run(_send())
check("a later successful send refreshes the stamp",
      notify_mod.last_notify_success() > first)

# The dashboard-facing surface: summary carries the heartbeat, honest-None
# when nothing was ever stamped.
r = client.get("/api/reports/summary")
check("summary is 200", r.status_code == 200)
body = r.json()
check("summary exposes last_notify_success_at",
      body.get("last_notify_success_at") is not None)
check("summary exposes the age in minutes (just stamped → < 5)",
      isinstance(body.get("notify_heartbeat_minutes_ago"), (int, float))
      and body["notify_heartbeat_minutes_ago"] < 5)

_STAMP.unlink()
body = client.get("/api/reports/summary").json()
check("no stamp ever → None, never a lying 0",
      body.get("last_notify_success_at") is None
      and body.get("notify_heartbeat_minutes_ago") is None)

# An old stamp reads as stale minutes, not as "healthy".
_STAMP.write_text((datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(), encoding="utf-8")
body = client.get("/api/reports/summary").json()
check("a 6-hour-old stamp reads as ~360 minutes stale",
      body["notify_heartbeat_minutes_ago"] is not None and body["notify_heartbeat_minutes_ago"] > 350)

print()
if failures:
    print(f"RESULT: {len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("RESULT: notify heartbeat checks OK")
