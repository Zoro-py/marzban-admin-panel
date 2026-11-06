"""Round-2 R1 review — regression tests for the bugs the critical read found.

Covers (docs/ZCODE_SHOPBOT_REVIEW.md):

  * R1-1  handle_other crashed with NameError on every voice note / sticker /
          video (the T7 money-words branch read an undefined `text`); a
          non-text message must get the ordinary «متوجه نشدم» answer.
  * R1-2  A stale inline volume screen (state lost to a restart) silently
          lost «سرویس جداگانه» — the volume callback now carries the
          force-new flag, so the re-seeded order still targets a NEW service.
  * R1-7  A stale wallet-confirm button is answered AND its old buttons are
          removed (reply_markup=None), same as the cancel path.
  * R1-16 A stale «همین مبلغ» tap with no flow state opens «حساب من» (the
          debt door) instead of «متوجه نشدم».
  * ROUTE The old base's (2b57db2) callback_data set — renew:/svcname:/
          autorenew:/debt:wallet/debt:receipt/debt:wallet:yes/debt:cancel —
          still matches a registered pattern in bot.py (old chats must never
          hold a dead button).

Run (from shopbot/):
  ../bot/venv/Scripts/python test_r1_review_fixes.py
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
    "payg_enabled": False, "payg_rate_per_gb": 0,
    "has_accounts": True, "has_payg_service": False,
}

ORDER = {
    "order_id": 77, "creates_new": True, "data_limit_gb": 30,
    "duration_days": 30, "price": 300_000, "balance": 0,
    "shortfall": 300_000, "payable_from_wallet": False,
    "card_number": "6037-9911-2222-3333", "card_holder": None,
    "approval_eta_minutes": 30,
}

captured: dict = {}


async def fake_session(update) -> dict:
    return dict(SESSION)


class StubBackend:
    async def get(self, path, params=None):
        if path == "/api/shop/bot/wallet":
            return []
        raise AssertionError(path)

    async def post(self, path, json=None, timeout: float = 20):
        if path == "/api/shop/bot/orders":
            captured["order_payload"] = dict(json or {})
            return dict(ORDER)
        raise AssertionError(path)


shop.backend = StubBackend()
shop._session = fake_session  # type: ignore[assignment]


async def main() -> None:
    # ── R1-1: a voice note / sticker must be answered, not crash ──────────
    (upd, context), msg = make_update(text=None)
    await shop.handle_other(upd, context)  # must not raise NameError
    check("handle_other (voice/sticker): answered, no NameError",
          len(msg.replies) == 1 and "متوجه نشدم" in msg.replies[0][0], repr(msg.replies))

    # ── R1-2: the volume callback carries the force-new flag ─────────────
    class FakeQuery:
        def __init__(self, data: str, msg: StubMessage):
            self.data = data
            self._msg = msg

        async def answer(self, *a, **k):
            return None

    for cb, expected in (("vol:30:f", True), ("vol:30", False)):
        captured.clear()
        (upd, context), msg = make_update(cb_data=None)
        upd.callback_query = FakeQuery(cb, msg)
        upd.effective_message = msg
        context.user_data.clear()  # restart: every state gone, screen still up
        await shop.on_volume_choice(upd, context)
        payload = captured.get("order_payload", {})
        check(f"stale volume tap {cb!r}: order keeps force_new_service={expected}",
              payload.get("force_new_service") is expected, repr(payload))

    # And the rendered screens carry it: a «سرویس جداگانه» volume screen puts
    # :f on its volume buttons; the plain one does not.
    (upd, context), msg = make_update()
    await shop._render_volume_screen(upd, dict(SESSION), force_new=True)
    rows = msg.replies[-1][1].inline_keyboard
    cbs = [b.callback_data for row in rows for b in row]
    check("force-new volume screen marks its volume buttons with :f",
          any(c.endswith(":f") for c in cbs if c and c.startswith("vol:")), repr(cbs))
    (upd, context), msg = make_update()
    await shop._render_volume_screen(upd, dict(SESSION), force_new=False)
    rows = msg.replies[-1][1].inline_keyboard
    cbs = [b.callback_data for row in rows for b in row]
    check("plain volume screen carries no :f",
          not any(c and c.endswith(":f") for c in cbs), repr(cbs))

    # ── R1-7: a stale wallet confirm loses its buttons when answered ─────
    (upd, context), msg = make_update(cb_data="buy:confirm")
    await shop.on_buy_confirm(upd, context)
    check("stale buy:confirm answered «قدیمی شده» with markup cleared",
          any("قدیمی" in r[0] and r[1] is None for r in msg.replies), repr(msg.replies))

    # ── R1-16: a stale «همین مبلغ» opens the debt door ────────────────────
    SESSION["ledger_debt"] = 123_000
    (upd, context), msg = make_update(texts.DEBT_AMOUNT_AS_IS)
    context.user_data.clear()  # no flow state at all
    await shop.handle_text(upd, context)
    check("stale «همین مبلغ» → «حساب من» (the debt door), not «متوجه نشدم»",
          any("ماندهٔ پرداخت‌نشده" in r[0] for r in msg.replies), repr([r[0] for r in msg.replies]))
    SESSION["ledger_debt"] = 0

    # ── ROUTE: every callback the OLD base could emit still routes ────────
    bot_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "bot.py"), encoding="utf-8").read()
    patterns = re.findall(r"CallbackQueryHandler\(\w+,\s*pattern=r\"([^\"]+)\"\)", bot_src)
    check("bot.py callback patterns parsed", len(patterns) >= 12, repr(patterns))
    legacy = ["renew:30", "svcname:9", "autorenew:12",
              "debt:wallet", "debt:receipt", "debt:wallet:yes", "debt:cancel"]
    for cb in legacy:
        check(f"legacy callback {cb!r} still matches a registered pattern",
              any(re.search(p, cb) for p in patterns), repr(patterns))


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All R1-review-fix cases passed.")
