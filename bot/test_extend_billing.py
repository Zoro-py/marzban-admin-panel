"""Behavioral regression test for bot /extend's billing order (MONEY-ADJ-1):
the GB charge must be posted to /api/ledger BEFORE the /adjust call, and the
adjust must carry bill_added_gb=true — otherwise the added GB shows as
pending too and the next settle bills it a second time (the double-charge
that hit account 39 for 300,000 T). Telegram objects and the backend client
are mocked — the charge is CAPTURED, never posted.

Run from bot/:  venv/Scripts/python test_extend_billing.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("ADMIN_CHAT_ID", "777")
os.environ.setdefault("API_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ.setdefault("BOT_TOKEN", "")
os.environ.setdefault("SHOP_BOT_TOKEN", "")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import handlers.account as account  # noqa: E402
import handlers.common as common  # noqa: E402

failures: list[str] = []
ADMIN = 777


def check(label: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


class FakeBackend:
    """Captures posts in order so the charge-before-adjust contract is
    assertable, not assumed."""

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []

    async def get(self, url: str):
        if url == "/api/accounts":
            return [{"id": 9, "marzban_username": "acc1", "customer_id": 5,
                     "effective_rate": 5000.0, "rate_configured": True}]
        raise AssertionError(f"unexpected GET {url}")

    async def post(self, url: str, json=None):
        self.posts.append((url, json or {}))
        if url.endswith("/adjust"):
            return {"marzban_username": "acc1", "expire": 9999999999, "data_limit": 40 * 1024**3}
        return {"amount": json.get("amount")}


fake = FakeBackend()
account.backend = fake
common.backend = fake  # resolve_account reads through handlers.common.backend


def make_update(args: list[str]):
    message = SimpleNamespace(reply_text=lambda *a, **k: asyncio.sleep(0, result=None))
    replies: list[str] = []

    async def _reply(text, **kwargs):
        replies.append(text)

    message.reply_text = _reply
    upd = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=ADMIN),
                          effective_user=SimpleNamespace(id=ADMIN))
    ctx = SimpleNamespace(args=args)
    return upd, ctx, replies


async def main() -> None:
    # 1) the billing path: /extend acc1 30 10 → charge FIRST, then adjust
    #    carrying bill_added_gb=true
    upd, ctx, replies = make_update(["acc1", "30", "10"])
    await account.extend_command(upd, ctx)
    urls = [u for u, _ in fake.posts]
    check("charge posted before adjust (order matters for the double-bill fix)",
          urls == ["/api/ledger", "/api/accounts/9/adjust"])
    check("charge is the GB charge at the effective rate",
          fake.posts[0][1]["amount"] == 50000.0 and fake.posts[0][1]["type"] == "charge")
    check("adjust carries bill_added_gb=true", fake.posts[1][1].get("bill_added_gb") is True)
    check("adjust still carries the size delta", fake.posts[1][1].get("extend_gb") == 10)
    check("operator told the amount was charged", any("charged" in r for r in replies))

    # 2) days-only: never billed, no flag
    fake.posts.clear()
    upd, ctx, _ = make_update(["acc1", "15"])
    await account.extend_command(upd, ctx)
    check("days-only adjust posts no charge and no flag",
          len(fake.posts) == 1 and fake.posts[0][0].endswith("/adjust")
          and "bill_added_gb" not in fake.posts[0][1])

    # 3) unassigned account: adjust without billing (unchanged semantics)
    fake.posts.clear()

    async def _get_unassigned(url):
        if url == "/api/accounts":
            return [{"id": 10, "marzban_username": "lonely", "customer_id": None, "group_id": None,
                     "effective_rate": 5000.0, "rate_configured": True}]
        raise AssertionError(url)

    fake.get = _get_unassigned  # type: ignore[method-assign]
    upd, ctx, replies = make_update(["lonely", "30", "5"])
    await account.extend_command(upd, ctx)
    check("unassigned account: adjust only, no charge, no flag",
          len(fake.posts) == 1 and fake.posts[0][0].endswith("/adjust")
          and "bill_added_gb" not in fake.posts[0][1])
    check("unassigned is said out loud", any("unassigned" in r for r in replies))


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All /extend billing-order cases passed.")
