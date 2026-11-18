"""Buy-flow regression tests (feat/shopbot-ux-rebuild) — owner decision
2026-11-18: NO MORE AUTO NAMING. The customer types their own Marzban
username; the backend uniquifies a taken one (ali → ali-2) and reports it.

  * Volume choice = INLINE buttons (popular sizes + «حجم دلخواه»), free text
    still accepted; the main menu reply keyboard stays visible underneath.
    NO «نام کاربری دلخواه» button anymore — the ask is mandatory where it
    matters (creates_new) and absent where it doesn't (extend in place).
  * creates_new (newcomer / سرویس جداگانه): order first, then the MANDATORY
    username ask («بدون نام» is retired — typing it gets the retired line,
    not an auto-named order), then the payment screen.
  * The taken-name path no longer bounces: the backend resolves ali-2 and
    reports was_taken → the payment screen says so.
  * Extend-in-place buys and ren: card renewals skip the ask entirely.
  * PAYG's name ask is mandatory too (no «بدون نام»).
  * The naming hint: a multi-service customer with an unnamed service is
    nudged ONCE toward «⋯ بیشتر» → «✏️ نام سرویس».

Run (from shopbot/):
  ../bot/venv/Scripts/python test_buy_flow.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
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

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.replies.append((text, reply_markup))


def make_update(text: str | None = None, user_id: int = 555, cb_data: str | None = None):
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
            data=cb_data, answer=_answer, edit_message_text=msg.edit_message_text, message=msg)
    context = SimpleNamespace(args=[], user_data={}, chat_data={})
    return (upd, context), msg


SESSION = {
    "balance": 0, "trial_available": False, "shop_name": None, "support_handle": None,
    "is_blocked": False, "is_open": True,
    "min_topup": 50_000, "max_topup": 5_000_000, "card_number": "6037-9911-2222-3333",
    "card_holder": None, "approval_eta_minutes": 30,
    "price_per_gb": 10_000, "min_gb": 5, "max_gb": 100, "plan_duration_days": 30,
    "ledger_debt": 0, "debt_breakdown": [], "debt_exact": True,
    "payg_enabled": True, "payg_rate_per_gb": 5_000, "has_payg_service": False,
    "has_accounts": False,
}
STATE = {
    "session": dict(SESSION),
    "creates_new": True,
    "order_posts": [],
    "username_posts": [],
    "payg_posts": [],
    "rows": [],
}
NOW = int(time.time())


async def fake_session(update) -> dict:
    return dict(STATE["session"])


class StubBackend:
    async def get(self, path, params=None):
        if path == "/api/shop/bot/accounts":
            return [dict(r) for r in STATE["rows"]]
        raise AssertionError(path)

    async def post(self, path, json=None, timeout: float = 20):
        if path == "/api/shop/bot/orders":
            STATE["order_posts"].append(dict(json or {}))
            return {
                "order_id": 77, "creates_new": STATE["creates_new"], "data_limit_gb": 10,
                "duration_days": 30, "price": 100_000, "balance": 0,
                "shortfall": 100_000, "payable_from_wallet": False,
                "card_number": "6037-9911-2222-3333", "card_holder": None,
                "approval_eta_minutes": 30,
            }
        if path == "/api/shop/bot/orders/77/username":
            STATE["username_posts"].append(dict(json or {}))
            chosen = (json or {}).get("name", "")
            taken = chosen == "ali"
            final = "ali-2" if taken else chosen
            return {"ok": True, "requested_username": final, "was_taken": taken}
        if path == "/api/shop/bot/payg/start":
            STATE["payg_posts"].append(dict(json or {}))
            return {}
        raise AssertionError(path)


shop.backend = StubBackend()
shop._session = fake_session  # type: ignore[assignment]


def reply_kb_labels(msg) -> list[str]:
    kb = msg.replies[-1][1]
    if kb is None or not hasattr(kb, "keyboard"):
        return []
    return [b.text for row_ in kb.keyboard for b in row_]


def inline_buttons(msg) -> list[tuple[str, str]]:
    kb = msg.replies[-1][1]
    if kb is None or not hasattr(kb, "inline_keyboard"):
        return []
    return [(b.text, b.callback_data or (b.url or "")) for row_ in kb.inline_keyboard for b in row_]


async def tap_volume(msg, ctx, volume="10"):
    msg.text = volume
    await shop.handle_text(msg.parent[0], msg.parent[1])


async def main() -> None:
    # ── 1. volume screen: no username button at all, any customer ────────
    STATE["session"] = {**SESSION, "has_accounts": False}
    (upd, context), msg = make_update(texts.MENU_BUY)
    await shop.handle_text(upd, context)
    check("newcomer's volume screen has NO «نام کاربری دلخواه» button",
          not any(c == "uname:ask" for _, c in inline_buttons(msg)), repr(inline_buttons(msg)))
    STATE["session"] = {**SESSION, "has_accounts": True}
    (upd, context), msg = make_update(texts.MENU_BUY)
    await shop.handle_text(upd, context)
    check("extender's volume screen has NO username button either",
          not any(c == "uname:ask" for _, c in inline_buttons(msg)), repr(inline_buttons(msg)))
    STATE["session"] = dict(SESSION)

    # ── 1b. the volume screen reads at a glance (owner report 2026-11-18:
    #        «این منو خیلی گیج کننده است») ──────────────────────────────────
    STATE["session"] = {**SESSION, "has_accounts": True}
    (upd, context), msg = make_update(texts.MENU_BUY)
    await shop.handle_text(upd, context)
    btns = inline_buttons(msg)
    labels = [t for t, _ in btns]
    check("every volume button carries its OWN price (تومان on the label)",
          sum(1 for t in labels if "گیگ" in t) >= 5
          and all("تومان" in t for t in labels if "گیگ" in t and "دلخواه" not in t),
          repr(labels))
    check("the two special flows are NOT rows shared with volumes",
          all(len(r_) == 1 for r_ in msg.replies[-1][1].inline_keyboard
              if any("جداگانه" in b.text or "مصرفی" in b.text for b in r_)),
          repr(msg.replies[-1][1].inline_keyboard))
    check("an extender's prompt says the buy ADDS to their current service",
          "اضافه می‌شود" in msg.replies[-1][0], repr(msg.replies[-1][0]))
    STATE["session"] = dict(SESSION)
    (upd, context), msg = make_update(texts.MENU_BUY)
    await shop.handle_text(upd, context)
    check("a newcomer's prompt does NOT promise an add-to-existing",
          "اضافه می‌شود" not in msg.replies[-1][0], repr(msg.replies[-1][0]))

    # ── 2. newcomer: order first, then the MANDATORY ask ──────────────────
    STATE["order_posts"].clear()
    STATE["session"] = dict(SESSION)
    (upd, context), msg = make_update(texts.MENU_BUY)
    msg.parent = (upd, context)
    await shop.handle_text(upd, context)
    await tap_volume(msg, context)
    check("newcomer volume tap → the mandatory username ask",
          any("نام کاربری" in r[0] for r in msg.replies[-1:]), repr([r[0] for r in msg.replies]))
    check("ask state reached, order created BEFORE the name",
          context.user_data.get(shop._STATE) == shop._STATE_AWAITING_PREFERRED_NAME
          and context.user_data.get(shop._ORDER_ID) == 77, repr(context.user_data))
    check("order POST carried no username (naming is the next step)",
          not STATE["order_posts"][-1].get("marzban_username"), repr(STATE["order_posts"]))
    kb_labels = reply_kb_labels(msg)
    check("the ask has NO «بدون نام» button", texts.SKIP_NAME not in kb_labels, repr(kb_labels))

    # ── 3. invalid name → the rule, state kept ────────────────────────────
    msg.text = "فاطمه"
    await shop.handle_text(upd, context)
    check("Persian name → the invalid-name reply (no crash)",
          msg.replies[-1][0] == texts.USERNAME_INVALID, repr(msg.replies[-1][0]))
    check("...state kept for a retry", context.user_data.get(shop._STATE) == shop._STATE_AWAITING_PREFERRED_NAME)

    # ── 4. «بدون نام» is RETIRED ──────────────────────────────────────────
    msg.text = texts.SKIP_NAME
    await shop.handle_text(upd, context)
    check("«بدون نام» tap → the retired line, never an auto-named order",
          msg.replies[-1][0] == texts.AUTO_NAME_RETIRED, repr(msg.replies[-1][0]))
    check("...state kept", context.user_data.get(shop._STATE) == shop._STATE_AWAITING_PREFERRED_NAME)

    # ── 5. valid name → username endpoint → payment screen ────────────────
    STATE["username_posts"].clear()
    msg.text = "fateme"
    await shop.handle_text(upd, context)
    check("the answer hits POST /orders/77/username",
          STATE["username_posts"] and STATE["username_posts"][-1]["name"] == "fateme",
          repr(STATE["username_posts"]))
    check("payment screen reached after the name",
          context.user_data.get(shop._STATE) == shop._STATE_AWAITING_RECEIPT, repr(context.user_data))
    check("no taken-note on a free name", not any("گرفته شده بود" in r[0] for r in msg.replies))

    # ── 6. taken name → uniquified, the payment screen SAYS the final ─────
    STATE["order_posts"].clear()
    STATE["username_posts"].clear()
    (upd, context), msg = make_update(texts.MENU_BUY)
    msg.parent = (upd, context)
    await shop.handle_text(upd, context)
    await tap_volume(msg, context)
    msg.text = "ali"  # the stub treats "ali" as taken → ali-2
    await shop.handle_text(upd, context)
    check("taken name → payment screen reached (no bounce)",
          context.user_data.get(shop._STATE) == shop._STATE_AWAITING_RECEIPT, repr(context.user_data))
    check("the screen says the final name and that it changed",
          any("ali-2" in r[0] and "گرفته شده بود" in r[0] for r in msg.replies[-1:]),
          repr([r[0] for r in msg.replies]))

    # ── 7. CANCEL during the ask cancels the pending buy ──────────────────
    STATE["order_posts"].clear()
    (upd, context), msg = make_update(texts.MENU_BUY)
    msg.parent = (upd, context)
    await shop.handle_text(upd, context)
    await tap_volume(msg, context)
    msg.text = texts.CANCEL
    await shop.handle_text(upd, context)
    check("CANCEL during the ask → «لغو شد», state cleared",
          any("لغو" in r[0] for r in msg.replies[-1:])
          and not context.user_data.get(shop._STATE), repr((msg.replies, context.user_data)))

    # ── 8. extend-in-place: NO ask ─────────────────────────────────────────
    STATE["creates_new"] = False
    STATE["session"] = {**SESSION, "has_accounts": True}
    STATE["order_posts"].clear()
    (upd, context), msg = make_update(texts.MENU_BUY)
    msg.parent = (upd, context)
    await shop.handle_text(upd, context)
    await tap_volume(msg, context)
    check("extender volume tap → straight to the payment screen (no ask)",
          context.user_data.get(shop._STATE) == shop._STATE_AWAITING_RECEIPT
          and not any("نام کاربری" in r[0] for r in msg.replies[-1:]), repr([r[0] for r in msg.replies]))
    STATE["creates_new"] = True
    STATE["session"] = dict(SESSION)

    # ── 9. PAYG: the name ask is mandatory too ─────────────────────────────
    (upd, context), msg = make_update(texts.PAYG_ON_BUY)
    msg.parent = (upd, context)
    await shop.handle_text(upd, context)
    check("PAYG intro+ask sent, no «بدون نام» button",
          any("نام کاربری" in r[0] for r in msg.replies)
          and texts.SKIP_NAME not in reply_kb_labels(msg), repr(reply_kb_labels(msg)))
    context.user_data[shop._STATE] = shop._STATE_AWAITING_PAYG_USERNAME
    context.user_data[shop._PENDING_PAYG_PAYLOAD] = {"telegram_username": "ali_t"}
    msg.text = texts.SKIP_NAME
    await shop.handle_text(upd, context)
    check("PAYG «بدون نام» → the retired line", msg.replies[-1][0] == texts.AUTO_NAME_RETIRED,
          repr(msg.replies[-1][0]))
    msg.text = "fateme"
    await shop.handle_text(upd, context)
    check("PAYG valid name → start carries marzban_username",
          STATE["payg_posts"] and STATE["payg_posts"][-1].get("marzban_username") == "fateme",
          repr(STATE["payg_posts"]))

    # ── 10. the naming hint: multi-service + unnamed → once ───────────────
    def row(account_id, nick):
        return {"order_id": account_id, "source": "shop", "account_id": account_id,
                "service_name": nick, "marzban_username": f"shop{account_id}",
                "data_limit_gb": 30, "used_traffic": 5 * 1024 ** 3, "data_limit": 30 * 1024 ** 3,
                "expire": NOW + 20 * 86400, "status": "active",
                "subscription_url": f"https://sub.example.com/s/{account_id}",
                "renews_here": False, "can_renew": True}
    STATE["rows"] = [row(11, None), row(12, "موبایل من")]
    STATE["session"] = {**SESSION, "has_accounts": True, "per_account_renewal": True}
    (upd, context), msg = make_update(texts.MENU_ACCOUNTS)
    msg.parent = (upd, context)
    await shop.handle_text(upd, context)
    check("unnamed multi-service → the naming hint appears",
          any("✏️ نام سرویس" in r[0] for r in msg.replies), repr([r[0] for r in msg.replies]))
    (upd2, context2), msg2 = make_update(texts.MENU_ACCOUNTS)
    msg2.parent = (upd2, context2)
    context2.chat_data.update(context.chat_data)
    await shop.handle_text(upd2, context2)
    check("the hint shows ONCE (chat_data flag)",
          not any("✏️ نام سرویس" in r[0] for r in msg2.replies), repr([r[0] for r in msg2.replies]))
    STATE["rows"] = [row(11, "تنها سرویس")]
    (upd3, context3), msg3 = make_update(texts.MENU_ACCOUNTS)
    msg3.parent = (upd3, context3)
    await shop.handle_text(upd3, context3)
    check("a fully-named customer never sees the hint",
          not any("✏️ نام سرویس" in r[0] for r in msg3.replies), repr([r[0] for r in msg3.replies]))
    STATE["session"] = dict(SESSION)


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All shopbot buy-flow cases passed.")
