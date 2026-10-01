"""Regression tests for shopbot's shop-link deep-link flow (2026-10-01):
/start shoplnk_<token> must claim the operator's invite and land on the link
welcome + menu, and every failure shape (404 expired/unknown, 409 conflicting
binding, server error) must produce its own polite text — never a
fall-through to the stranger's shop welcome. Telegram objects and the backend
client are stubbed — nothing real is touched. Mirror of delegate_bot's
test_invite_claim.py, which tests the delegate claim the same way.

Run (from shopbot/, using bot's venv — same interpreter family):
  ../bot/venv/Scripts/python test_shop_link_claim.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("SHOP_BOT_TOKEN", "")
os.environ.setdefault("SHOP_BOT_API_KEY", "k")
os.environ.setdefault("API_BASE_URL", "https://panel.test")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import texts  # noqa: E402
import handlers.shop as shop  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class StubMessage:
    def __init__(self):
        self.replies: list[tuple] = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append((text, reply_markup))


def make_update(args: list[str], user_id: int = 555, username: str = "ali_t") -> tuple[SimpleNamespace, StubMessage]:
    msg = StubMessage()
    upd = SimpleNamespace(
        effective_message=msg,
        effective_user=SimpleNamespace(id=user_id, username=username),
    )
    context = SimpleNamespace(args=args, user_data={})
    return (upd, context), msg


class StubBackend:
    def __init__(self):
        self.claim_calls: list[tuple[str, dict]] = []
        self.claim_result: dict = {"customer_name": "Ali", "accounts_linked": 2}
        self.claim_error = None

    async def post(self, path: str, json=None):
        if path == "/api/shop/bot/claim-link":
            self.claim_calls.append((path, json))
            if self.claim_error is not None:
                raise shop.ShopApiError(self.claim_error[0], status=self.claim_error[1])
            return dict(self.claim_result)
        raise AssertionError(path)


async def fake_session(update) -> dict:
    return {"balance": 0, "trial_available": False, "shop_name": None, "support_handle": None}


stub = StubBackend()
shop.backend = stub
session_calls: list = []


async def recording_session(update) -> dict:
    session_calls.append(update)
    return await fake_session(update)


shop._session = recording_session  # type: ignore[assignment]


async def main() -> None:
    # 1) happy path: the deep link claims the invite and lands on the link
    #    welcome + menu.
    (upd, context), msg = make_update(["shoplnk_abc123"])
    await shop.start(upd, context)
    check("claim POSTed with the stripped token, telegram_id and username",
          stub.claim_calls == [("/api/shop/bot/claim-link", {
              "token": "abc123", "telegram_id": 555, "telegram_username": "ali_t"})])
    check("success shows the link welcome (customer name + count) and the menu",
          len(msg.replies) == 1 and "Ali" in msg.replies[0][0] and "۲" in msg.replies[0][0]
          and msg.replies[0][1] is not None)

    # 2) 404 (unknown / used / expired token): dedicated text, no menu, and
    #    crucially NOT the stranger's buy-something welcome.
    stub.claim_error = ("invalid", 404)
    (upd, context), msg = make_update(["shoplnk_dead"])
    await shop.start(upd, context)
    check("404 shows LINK_INVALID_OR_EXPIRED", msg.replies[0][0] == texts.LINK_INVALID_OR_EXPIRED)
    check("404 attaches no menu and never shows the shop welcome",
          msg.replies[0][1] is None and "خوش آمدید" not in msg.replies[0][0])

    # 3) 409 (this Telegram account already carries another customer).
    stub.claim_error = ("linked", 409)
    (upd, context), msg = make_update(["shoplnk_dup"])
    await shop.start(upd, context)
    check("409 shows LINK_ALREADY_CONNECTED", msg.replies[0][0] == texts.LINK_ALREADY_CONNECTED)

    # 4) a genuine server error stays generic_error (not a link text).
    stub.claim_error = ("boom", 500)
    (upd, context), msg = make_update(["shoplnk_x"])
    await shop.start(upd, context)
    check("500 shows the generic error", msg.replies[0][0] == texts.generic_error(None))
    stub.claim_error = None

    # 5) payload-less /start: unchanged legacy behavior (session path), no
    #    claim POST, no _session call for the shoplnk case either.
    claims_before, sessions_before = len(stub.claim_calls), len(session_calls)
    (upd, context), msg = make_update([])
    await shop.start(upd, context)
    check("payload-less /start uses the session path exactly as before",
          len(stub.claim_calls) == claims_before and len(session_calls) == sessions_before + 1
          and msg.replies[0][0].startswith("سلام"))

    # 6) a non-shoplnk payload does not trigger the claim path either.
    (upd, context), msg = make_update(["something-else"])
    await shop.start(upd, context)
    check("non-shoplnk argument never reaches the claim endpoint",
          len(stub.claim_calls) == claims_before and len(session_calls) == sessions_before + 2)

    # 7) /start shoplnk_ (empty token after the prefix) still gets a polite
    #    404 text from the backend — not a crash, not the shop welcome.
    stub.claim_error = ("invalid", 404)
    (upd, context), msg = make_update(["shoplnk_"])
    await shop.start(upd, context)
    check("bare shoplnk_ prefix is sent to the backend and refused politely (404 text)",
          len(stub.claim_calls) == claims_before + 1
          and msg.replies[0][0] == texts.LINK_INVALID_OR_EXPIRED)


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All shopbot shop-link claim cases passed.")
