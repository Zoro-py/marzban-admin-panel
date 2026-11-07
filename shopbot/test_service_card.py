"""T3 service-card regression tests (feat/shopbot-ux-rebuild):

  * «سرویس‌های من» = ONE message per service, each card carrying its own
    inline buttons: 🔄 تمدید (existing renew: callback → the same renew path
    the expiry warnings use), 🔗 لینک اشتراک (URL), ⋯ بیشتر (name +
    auto-renew). NO trailing wallet summary — money lives in «حساب من».
  * An ended service (volume gone OR days up) puts تمدید FIRST and says what
    happened and what one tap does.
  * An unlimited service gets NO تمدید (there is no volume to renew).
  * The suspended badge still renders (F11 regression).
  * «⋯ بیشتر» opens the rarer actions: ✏️ نام سرویس + ⚡ تمدید خودکار.

Telegram objects and the backend client are stubbed. Same script style.

Run (from shopbot/):
  ../bot/venv/Scripts/python test_service_card.py
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
    print(f"[{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


class StubMessage:
    def __init__(self):
        self.replies: list[tuple] = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append((text, reply_markup))


def make_update(user_id: int = 555, cb_data: str | None = None):
    msg = StubMessage()
    upd = SimpleNamespace(
        effective_message=msg,
        message=msg,
        effective_user=SimpleNamespace(id=user_id, username="ali_t",
                                       first_name="علی", last_name=None),
    )
    if cb_data is not None:
        async def _answer(*a, **k):
            return None
        async def _edit(text, reply_markup=None, **k):
            msg.replies.append((text, reply_markup))
        upd.callback_query = SimpleNamespace(
            data=cb_data, answer=_answer, edit_message_text=_edit, message=msg)
    context = SimpleNamespace(args=[], user_data={}, chat_data={})
    return (upd, context), msg


SESSION = {
    "balance": 0, "trial_available": False, "shop_name": None, "support_handle": None,
    "is_blocked": False, "is_open": True,
    "min_topup": 50_000, "max_topup": 5_000_000, "card_number": None,
    "card_holder": None, "approval_eta_minutes": 30,
    "price_per_gb": 10_000, "min_gb": 5, "max_gb": 100, "plan_duration_days": 30,
    "ledger_debt": 0, "debt_breakdown": [], "debt_exact": True,
    "payg_enabled": False, "payg_rate_per_gb": 0,
    "has_accounts": True, "has_payg_service": False,
}

ROWS: list[dict] = []


async def fake_session(update) -> dict:
    return dict(SESSION)


class StubBackend:
    async def get(self, path, params=None):
        if path == "/api/shop/bot/accounts":
            return [dict(r) for r in ROWS]
        raise AssertionError(path)

    async def post(self, path, json=None, timeout: float = 20):
        raise AssertionError(path)


shop.backend = StubBackend()
shop._session = fake_session  # type: ignore[assignment]


def row(**over) -> dict:
    import time
    r = {
        "order_id": 1, "source": "shop", "account_id": 11,
        "service_name": None, "marzban_username": "shop10",
        "data_limit_gb": 30, "used_traffic": 10 * 1024 ** 3,
        "data_limit": 30 * 1024 ** 3,
        "expire": int(time.time()) + 20 * 86400,
        "status": "active", "subscription_url": "https://sub.example.com/s/abc",
    }
    r.update(over)
    return r


def buttons_of(msg) -> list[tuple[str, str]]:
    kb = msg.replies[-1][1]
    if kb is None or not hasattr(kb, "inline_keyboard"):
        return []
    return [(b.text, b.callback_data or (b.url or "")) for row_ in kb.inline_keyboard for b in row_]


async def main() -> None:
    # ── 1. one message per service, no wallet trailer ────────────────────
    ROWS.clear()
    ROWS.append(row())
    (upd, context), msg = make_update()
    await shop.show_accounts(upd, context)
    check("one service = exactly ONE message", len(msg.replies) == 1, repr(len(msg.replies)))
    check("no trailing wallet summary",
          "کیف پول" not in msg.replies[-1][0] and "موجودی" not in msg.replies[-1][0],
          repr(msg.replies[-1][0]))

    # ── 2. card buttons: تمدید، لینک، بیشتر ───────────────────────────────
    btns = buttons_of(msg)
    check("card carries تمدید / لینک اشتراک / ⋯ بیشتر in that order",
          [t for t, _ in btns] == ["🔄 تمدید", "🔗 لینک اشتراک", "⋯ بیشتر"], repr(btns))
    check("تمدید callback = renew:30 (existing renew path)",
          btns[0][1] == "renew:30", repr(btns[0]))
    check("لینک is a URL button", btns[1][1] == "https://sub.example.com/s/abc")
    check("بیشتر callback = svcmore:11", btns[2][1] == "svcmore:11")

    # ── 3. ended service: تمدید first + the one-tap sentence ─────────────
    ROWS.clear()
    ROWS.append(row(used_traffic=30 * 1024 ** 3))  # volume exhausted
    (upd, context), msg = make_update()
    await shop.show_accounts(upd, context)
    text = msg.replies[-1][0]
    btns = buttons_of(msg)
    check("exhausted card: «حجم سرویس تمام شد» + the one-tap sentence",
          "حجم سرویس تمام شد" in text and "تمدید" in text and "فعال" in text, repr(text))
    check("exhausted card: تمدید is the FIRST button",
          btns[0][0] == "🔄 تمدید", repr(btns))

    ROWS.clear()
    ROWS.append(row(expire=int(__import__("time").time()) - 3600))  # time over
    (upd, context), msg = make_update()
    await shop.show_accounts(upd, context)
    check("time-expired card: «تمام شده» + the one-tap sentence, تمدید first",
          "تمام شده" in msg.replies[-1][0] and "فعال" in msg.replies[-1][0]
          and buttons_of(msg)[0][0] == "🔄 تمدید", repr(msg.replies[-1][0]))

    # ── 4. unlimited service: no تمدید ───────────────────────────────────
    ROWS.clear()
    ROWS.append(row(data_limit=None, data_limit_gb=0))
    (upd, context), msg = make_update()
    await shop.show_accounts(upd, context)
    check("unlimited card: نامحدود rendered, NO تمدید button",
          "نامحدود" in msg.replies[-1][0]
          and all(c != "renew:0" and not c.startswith("renew:") for _, c in buttons_of(msg)),
          repr((msg.replies[-1][0], buttons_of(msg))))

    # ── 5. suspended badge still renders ─────────────────────────────────
    ROWS.clear()
    ROWS.append(row(status="disabled"))
    (upd, context), msg = make_update()
    await shop.show_accounts(upd, context)
    check("suspended card carries the plain-words badge",
          texts.STATUS_SUSPENDED_BADGE in msg.replies[-1][0], repr(msg.replies[-1][0]))

    # ── 6. «⋯ بیشتر» panel: name + auto-renew ────────────────────────────
    ROWS.clear()
    ROWS.append(row())
    (upd, context), msg = make_update(cb_data="svcmore:11")
    await shop.on_service_more(upd, context)
    btns = buttons_of(msg)
    check("بیشتر panel offers نام سرویس (svcname:) and تمدید خودکار (autorenew:)",
          any(c == "svcname:11" for _, c in btns) and any(c == "autorenew:11" for _, c in btns),
          repr(btns))

    # ── 6a. auto-renew is a shop-wallet feature: the panel offers it only for
    #        shop-bought services (live 2026-11-07: «سرویس اصلی», an
    #        operator-managed account, got the button and a «مشکل فنی»)
    ROWS.clear()
    ROWS.append(row(account_id=11, source="shop"))
    ROWS.append(row(account_id=12, source="linked", order_id=None))
    (upd, context), msg = make_update(cb_data="svcmore:12")
    await shop.on_service_more(upd, context)
    btns = buttons_of(msg)
    check("linked service: no تمدید خودکار button", not any(c.startswith("autorenew:") for _, c in btns), repr(btns))
    check("linked service: نام سرویس still there", any(c == "svcname:12" for _, c in btns), repr(btns))
    # the backend's own verdict wins over the source guess: a shop-sourced row
    # the backend marks auto_renew_ok=False (customer-owned account that also
    # has shop orders — live «سرویس اصلی») gets no button
    ROWS.clear()
    ROWS.append(row(account_id=13, source="shop", auto_renew_ok=False))
    (upd, context), msg = make_update(cb_data="svcmore:13")
    await shop.on_service_more(upd, context)
    check("shop-sourced but auto_renew_ok=False: no تمدید خودکار",
          not any(c.startswith("autorenew:") for _, c in buttons_of(msg)), repr(buttons_of(msg)))
    ROWS.clear()
    ROWS.append(row(account_id=11, source="shop"))
    ROWS.append(row(account_id=12, source="linked", order_id=None))
    (upd, context), msg = make_update(cb_data="svcmore:11")
    await shop.on_service_more(upd, context)
    check("shop service keeps تمدید خودکار",
          any(c == "autorenew:11" for _, c in buttons_of(msg)), repr(buttons_of(msg)))

    # ── 6a2. a refusal the backend words in English is a calm Persian line,
    #         never the «مشکل فنی» text
    from api_client import ShopApiError
    refusals = ["This service can't be auto-renewed.", "This service is no longer active."]
    for refusal in refusals:
        class RefusingBackend(StubBackend):
            async def post(self, path, json=None, timeout: float = 20, _r=refusal):
                raise ShopApiError(_r, 400)
        shop.backend = RefusingBackend()
        (upd, context), msg = make_update(cb_data="autorenew:12")
        await shop.on_auto_renew(upd, context)
        last = msg.replies[-1][0]
        check(f"refusal «{refusal[:28]}…» -> friendly line, not generic error",
              last == texts.AUTO_RENEW_NOT_AVAILABLE, repr(last))
    shop.backend = StubBackend()

    # ── 6b. multi-service: renew button only on the card that renews ────
    ROWS.clear()
    ROWS.append(row(account_id=11, renews_here=True))
    ROWS.append(row(account_id=12, renews_here=False, used_traffic=30 * 1024 ** 3))
    (upd, context), msg = make_update()
    await shop.show_accounts(upd, context)
    first = [c for _, c in [(b.text, b.callback_data or "") for r_ in msg.replies[0][1].inline_keyboard for b in r_]]
    second = [c for _, c in [(b.text, b.callback_data or "") for r_ in msg.replies[1][1].inline_keyboard for b in r_]]
    check("target card keeps its renew button", any(c.startswith("renew:") for c in first), repr(first))
    check("non-target card has NO renew button", not any(c.startswith("renew:") for c in second), repr(second))
    check("non-target ended card does not promise a renew tap",
          texts.RENEW_HINT_ENDED not in msg.replies[1][0], repr(msg.replies[1][0]))
    check("non-target card keeps link + more", any(c.startswith("svcmore:") for c in second), repr(second))

    # ── 7. empty list unchanged ──────────────────────────────────────────
    ROWS.clear()
    (upd, context), msg = make_update()
    await shop.show_accounts(upd, context)
    check("no services → NO_ACCOUNTS", msg.replies[-1][0] == texts.NO_ACCOUNTS)


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All shopbot service-card cases passed.")
