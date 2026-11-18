"""Regression: an INVALID typed username must be answered, never crash.

Round A (2026-11-17): the invalid-name branch referenced
`texts.USERNAME_INVALID`, which did not exist — a customer typing a Persian
name got an AttributeError into on_error's generic reply. Round D-fix2
(2026-11-18, owner decision): the ask is now MANDATORY on every new-service
buy (no more auto naming), entered after the order via intent.creates_new;
«بدون نام» is retired (old keyboards get the retired line); a valid answer
hits POST /orders/{id}/username (which uniquifies a taken one) and lands on
the payment screen.

  * texts defines USERNAME_INVALID and AUTO_NAME_RETIRED.
  * Buy flow: volume tap → the ask (state + order id), invalid → the rule
    with the state kept, «بدون نام» → the retired line, valid → the
    username endpoint → the payment screen.
  * The PAYG ask: invalid → the same reply, no exception; valid → the start
    carries marzban_username.
  * The example printed in ASK_PREFERRED_NAME satisfies the rule it states.

Run (from shopbot/):
  ../bot/venv/Scripts/python test_invalid_username.py
"""

from __future__ import annotations

import asyncio
import os
import re
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
    print(f"[{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


class StubMessage:
    def __init__(self, text: str | None = None):
        self.text = text
        self.replies: list[tuple] = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append((text, reply_markup))


def make_update(text: str | None = None, user_id: int = 777, cb_data: str | None = None):
    msg = StubMessage(text)
    upd = SimpleNamespace(
        effective_message=msg,
        message=msg,
        effective_user=SimpleNamespace(id=user_id, username="ali_t",
                                       first_name="علی", last_name=None),
    )
    if cb_data is not None:
        async def _answer(*a, **k):
            return None
        upd.callback_query = SimpleNamespace(
            data=cb_data, answer=_answer, edit_message_text=msg.reply_text, message=msg)
    context = SimpleNamespace(args=[], user_data={}, chat_data={})
    return (upd, context), msg


SESSION = {
    "balance": 0, "trial_available": False, "shop_name": None, "support_handle": None,
    "is_blocked": False, "is_open": True,
    "min_topup": 50_000, "max_topup": 5_000_000, "card_number": "6037-9911-2222-3333",
    "card_holder": None, "approval_eta_minutes": 30, "phone": None,
    "price_per_gb": 10_000, "min_gb": 5, "max_gb": 100, "plan_duration_days": 30,
    "ledger_debt": 0, "debt_breakdown": [], "debt_exact": True,
    "payg_enabled": True, "payg_rate_per_gb": 5_000,
    "has_accounts": False, "has_payg_service": False,
}
STATE = {"username_posts": [], "payg_posts": []}


async def fake_session(update) -> dict:
    return dict(SESSION)


class StubBackend:
    async def get(self, path, params=None):
        raise AssertionError(path)

    async def post(self, path, json=None, timeout: float = 20):
        if path == "/api/shop/bot/orders":
            return {"order_id": 77, "creates_new": True, "data_limit_gb": 10,
                    "duration_days": 30, "price": 100_000, "balance": 0,
                    "shortfall": 100_000, "payable_from_wallet": False,
                    "card_number": "6037-9911-2222-3333", "card_holder": None,
                    "approval_eta_minutes": 30}
        if path == "/api/shop/bot/orders/77/username":
            STATE["username_posts"].append(dict(json or {}))
            chosen = (json or {}).get("name", "")
            taken = chosen == "ali"
            return {"ok": True, "requested_username": "ali-2" if taken else chosen,
                    "was_taken": taken}
        if path == "/api/shop/bot/payg/start":
            STATE["payg_posts"].append(dict(json or {}))
            return {}
        raise AssertionError(path)


shop.backend = StubBackend()
shop._session = fake_session  # type: ignore[assignment]


async def main() -> None:
    # ── 1. the text module defines the replies the handlers send ─────────
    check("texts.USERNAME_INVALID exists and is a single string",
          isinstance(texts.USERNAME_INVALID, str), "AttributeError on base")
    check("texts.AUTO_NAME_RETIRED exists",
          isinstance(texts.AUTO_NAME_RETIRED, str), "AttributeError on base")
    check("the invalid reply no longer promises «بدون نام» (auto naming is gone)",
          "بدون نام" not in texts.USERNAME_INVALID, repr(texts.USERNAME_INVALID))

    # ── 2. buy flow: volume tap → the mandatory ask ───────────────────────
    (upd, context), msg = make_update(texts.MENU_BUY)
    msg.parent = (upd, context)
    await shop.handle_text(upd, context)
    msg.text = "10"
    await shop.handle_text(upd, context)
    check("volume tap → the mandatory username ask",
          context.user_data.get(shop._STATE) == shop._STATE_AWAITING_PREFERRED_NAME
          and context.user_data.get(shop._ORDER_ID) == 77, repr(context.user_data))

    msg.text = "فاطمه"  # Persian — fails the [a-zA-Z0-9_-] pattern
    await shop.handle_text(upd, context)
    check("invalid name → the invalid-name reply (no crash)",
          msg.replies[-1][0] == texts.USERNAME_INVALID, repr(msg.replies[-1][0]))
    check("invalid name keeps the ask state (retry is possible)",
          context.user_data.get(shop._STATE) == shop._STATE_AWAITING_PREFERRED_NAME)

    msg.text = texts.SKIP_NAME
    await shop.handle_text(upd, context)
    check("«بدون نام» → the retired line, never an auto-named order",
          msg.replies[-1][0] == texts.AUTO_NAME_RETIRED, repr(msg.replies[-1][0]))

    msg.text = "fateme"
    await shop.handle_text(upd, context)
    check("valid name hits the username endpoint",
          STATE["username_posts"] and STATE["username_posts"][-1]["name"] == "fateme",
          repr(STATE["username_posts"]))
    check("then the payment screen (receipt await)",
          context.user_data.get(shop._STATE) == shop._STATE_AWAITING_RECEIPT,
          repr(context.user_data))

    # ── 3. PAYG username ask: invalid → answered; valid → start runs ─────
    STATE["payg_posts"].clear()
    (upd, context), msg = make_update("فاطمه")
    context.user_data[shop._STATE] = shop._STATE_AWAITING_PAYG_USERNAME
    context.user_data[shop._PENDING_PAYG_PAYLOAD] = {"telegram_username": "ali_t"}
    await shop.handle_text(upd, context)
    check("PAYG: invalid name → the invalid-name reply (no crash)",
          msg.replies[-1][0] == texts.USERNAME_INVALID, repr(msg.replies[-1][0]))
    check("PAYG: invalid name keeps the ask state",
          context.user_data.get(shop._STATE) == shop._STATE_AWAITING_PAYG_USERNAME)
    msg.text = "fateme"
    await shop.handle_text(upd, context)
    check("PAYG: valid name → start carries marzban_username",
          STATE["payg_posts"] and STATE["payg_posts"][-1].get("marzban_username") == "fateme",
          repr(STATE["payg_posts"]))

    # ── 4. the ask's own example satisfies the rule it states ────────────
    m = re.search(r"مثلاً\s+([A-Za-z0-9_.\-]+)", texts.ASK_PREFERRED_NAME)
    check("ASK_PREFERRED_NAME prints an example", m is not None,
          repr(texts.ASK_PREFERRED_NAME))
    if m:
        check("the printed example passes the username pattern itself",
              re.fullmatch(r"[a-zA-Z0-9_-]{3,30}", m.group(1)) is not None,
              repr(m.group(1)))


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All invalid-username cases passed.")
