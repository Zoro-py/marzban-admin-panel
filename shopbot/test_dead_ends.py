"""T7 dead-ends regression tests (feat/shopbot-ux-rebuild):

  * The phone-share prompt's keyboard carries the main-menu rows UNDER the
    share buttons — sharing a number can no longer strand the customer away
    from the menu (T0 trap #1).
  * A QUESTION typed while the bot waits for a receipt gets an answer (the
    nudge + menu), not «این را به‌عنوان رسید متوجه نشدم»; non-questions still
    get the receipt prompt.
  * Money words typed as free text («موجودی؟», «بدهی دارم؟») open «حساب من»
    instead of «متوجه نشدم» (T0 finding #6).
  * Malformed / stale inline callback data answers with «این دکمه قدیمی شده»
    — never a traceback, never silence.
  * The rename ask names its way back; the receipt prompt names all three
    accepted forms (photo / image file / tracking code) in one line.

Run (from shopbot/):
  ../bot/venv/Scripts/python test_dead_ends.py
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
    "balance": 40_000, "trial_available": False, "shop_name": None, "support_handle": None,
    "is_blocked": False, "is_open": True,
    "min_topup": 50_000, "max_topup": 5_000_000, "card_number": "6037-9911-2222-3333",
    "card_holder": None, "approval_eta_minutes": 30, "phone": None,
    "price_per_gb": 10_000, "min_gb": 5, "max_gb": 100, "plan_duration_days": 30,
    "ledger_debt": 0, "debt_breakdown": [], "debt_exact": True,
    "payg_enabled": False, "payg_rate_per_gb": 0,
    "has_accounts": True, "has_payg_service": False,
}

ORDER = {
    "order_id": 77, "creates_new": True, "data_limit_gb": 10,
    "duration_days": 30, "price": 100_000, "balance": 0,
    "shortfall": 100_000, "payable_from_wallet": False,
    "card_number": "6037-9911-2222-3333", "card_holder": None,
    "approval_eta_minutes": 30,
}


async def fake_session(update) -> dict:
    return dict(SESSION)


class StubBackend:
    async def get(self, path, params=None):
        if path == "/api/shop/bot/wallet":
            return []
        raise AssertionError(path)

    async def post(self, path, json=None, timeout: float = 20):
        if path == "/api/shop/bot/orders":
            return dict(ORDER)
        if path == "/api/shop/bot/topups":
            return {"reference_code": "ABCD", "status": "pending"}
        raise AssertionError(path)


shop.backend = StubBackend()
shop._session = fake_session  # type: ignore[assignment]


async def main() -> None:
    # ── 1. phone-share keyboard keeps the menu reachable ─────────────────
    context = SimpleNamespace(user_data={}, chat_data={})
    upd, msg = make_update()[0], None
    (upd, _ctx), msg = make_update()
    session = dict(SESSION)
    await shop._maybe_offer_phone_share(upd, context, session)
    kb = msg.replies[-1][1]
    rows = [[b.text for b in row] for row in kb.keyboard]
    flat_labels = [t for r in rows for t in r]
    check("phone prompt keeps its two buttons on row 1",
          rows[0] == [texts.SHARE_PHONE_BUTTON, texts.PHONE_LATER], repr(rows))
    check("phone prompt carries the main-menu rows underneath",
          any(texts.MENU_BUY in t or texts.MENU_BUY_RENEW in t for t in flat_labels[2:])
          and any(texts.MENU_HELP_SUPPORT in t for t in flat_labels[2:]), repr(rows))

    # ── 2. a question during the receipt wait gets an answer ─────────────
    (upd, context), msg = make_update(texts.MENU_BUY)
    await shop.handle_text(upd, context)
    msg.text = "10"
    await shop.handle_text(upd, context)          # → receipt-await state
    state_before = context.user_data.get(shop._STATE)
    check("receipt-await state reached", state_before == shop._STATE_AWAITING_RECEIPT)
    msg.text = "کی تایید می‌کنی؟"
    await shop.handle_text(upd, context)
    last = msg.replies[-1][0]
    check("a question gets the nudge, NOT the receipt prompt",
          "متوجه نشدم" in last and "رسید متوجه نشدم" not in last, repr(last))
    msg.text = "واریز کردم 100000 تومان کد 1234"
    await shop.handle_text(upd, context)
    last = msg.replies[-1][0]
    check("a receipt-looking text is still taken as the receipt",
          "دریافت شد" in last or "بررسی" in last, repr(last))

    # ── 3. money words as free text open «حساب من» ───────────────────────
    (upd, context), msg = make_update("موجودی چنده؟")
    await shop.handle_text(upd, context)
    check("«موجودی» → the account view",
          any("موجودی" in r[0] for r in msg.replies), repr([r[0] for r in msg.replies]))
    (upd, context), msg = make_update("بدهی دارم؟")
    await shop.handle_text(upd, context)
    check("«بدهی» → the account view",
          any("موجودی" in r[0] or "ماندهٔ پرداخت" in r[0] for r in msg.replies),
          repr([r[0] for r in msg.replies]))

    # ── 4. malformed inline callback data → the stale answer ─────────────
    (upd, context), msg = make_update(cb_data="svcname:")
    await shop.on_service_name(upd, context)
    check("malformed svcname callback → «قدیمی شده»",
          any("قدیمی" in r[0] for r in msg.replies), repr(msg.replies))
    (upd, context), msg = make_update(cb_data="svcmore:")
    await shop.on_service_more(upd, context)
    check("malformed svcmore callback → «قدیمی شده»",
          any("قدیمی" in r[0] for r in msg.replies), repr(msg.replies))

    # ── 4b. the OLD debt keyboard's cancel answers, never opens a flow ───
    SESSION["ledger_debt"] = 123_000
    SESSION["balance"] = 30_000
    (upd, context), msg = make_update(cb_data="debt:cancel")
    await shop.on_pay_debt(upd, context)
    check("debt:cancel restores the debt view (does NOT open the receipt ask)",
          any("ماندهٔ پرداخت‌نشده" in r[0] for r in msg.replies)
          and not any("واریز کنید" in r[0] for r in msg.replies), repr([r[0] for r in msg.replies]))
    SESSION["ledger_debt"] = 0
    SESSION["balance"] = 40_000

    # ── 5. the rename ask names its way back ─────────────────────────────
    check("ASK_SERVICE_NAME mentions the menu as the way back",
          "منو" in texts.ASK_SERVICE_NAME, repr(texts.ASK_SERVICE_NAME))

    # ── 6. the receipt prompt names all three accepted forms ─────────────
    pay = texts.topup_instructions(100_000, "6037-9911-2222-3333", None, 30)
    check("topup prompt: عکس / فایل / کد پیگیری in one line",
          "عکس" in pay and "فایل" in pay and "کد پیگیری" in pay, repr(pay))
    debt_pay = texts.debt_receipt_instructions(100_000, "6037-9911-2222-3333", None, 30)
    check("debt prompt: عکس / فایل / کد پیگیری",
          "عکس" in debt_pay and "فایل" in debt_pay and "کد پیگیری" in debt_pay, repr(debt_pay))


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All dead-ends cases passed.")
