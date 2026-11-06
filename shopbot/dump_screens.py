"""Round-2 R3 — screen transcript: what the customer actually SEES.

Drives the REAL handlers (handlers/shop.py) against stubbed Telegram +
backend — the same harness style as the tests — and writes
docs/SHOPBOT_SCREENS.md: every step prints the bot's message text exactly
(fenced block) and its buttons as a table (label → callback / URL), with the
tap count marked on every screen.

Personas (the customer states the review round asks for):

  P1 brand-new       trial-eligible stranger
  P2 one service     active 30 GB service + balance (wallet-covers buy,
                     card link, more panel, account/top-up)
  P3 two services    one active + one ENDED (renews_here on the ended one)
                     + one linked (no renew button there)
  P4 owes money      ledger debt 185,000, empty wallet (receipt route,
                     breakdown behind جزئیات)
  P5 owes + balance  same debt, wallet 120,000 → the ONE-tap wallet pay,
                     and what a second (stale) tap of that button does

Run (from shopbot/):
  ../bot/venv/Scripts/python dump_screens.py
Output: ../docs/SHOPBOT_SCREENS.md (regenerate after any copy/screen change).
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
from api_client import ShopApiError  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "SHOPBOT_SCREENS.md")


# ── stubs ─────────────────────────────────────────────────────────────────

class Msg:
    def __init__(self, text: str | None = None):
        self.text = text
        self.sent: list[tuple[str, object]] = []   # (text, markup-or-None)

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.sent.append((text, reply_markup))


class Query:
    """callback_query whose edits land on the SAME Msg (Telegram edits in place)."""

    def __init__(self, data: str, msg: Msg):
        self.data = data
        self._msg = msg

    async def answer(self, *a, **k):
        return None

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self._msg.sent.append((text, reply_markup))

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs):
        return None


class Context:
    def __init__(self):
        self.user_data: dict = {}
        self.chat_data: dict = {}
        self.args: list = []


SESSION_BASE = {
    "shop_name": "فروشگاه نمونه", "support_handle": "vpn_support",
    "is_blocked": False, "is_open": True,
    "price_per_gb": 10_000, "min_gb": 5, "max_gb": 100, "plan_duration_days": 30,
    "min_topup": 50_000, "max_topup": 5_000_000,
    "card_number": "6037-9911-2222-3333", "card_holder": None,
    "approval_eta_minutes": 30, "phone": None,
    "ledger_debt": 0.0, "debt_breakdown": [], "debt_exact": True,
    "trial_enabled": False, "trial_available": False, "trial_gb": 1, "trial_hours": 24,
    "payg_enabled": False, "payg_rate_per_gb": 0,
    "has_accounts": False, "has_payg_service": False,
}

ACCOUNT_ACTIVE = {
    "account_id": 101, "service_name": "گوشی خودم", "marzban_username": "shop10",
    "source": "shop", "used_traffic": int(12.4 * 1024**3), "data_limit": int(30 * 1024**3),
    "expire": None, "status": "active",
    "subscription_url": "https://sub.example.com/s/abc123",
}

ACCOUNT_ENDED = {
    "account_id": 102, "service_name": None, "marzban_username": "shop7",
    "source": "shop", "used_traffic": int(50 * 1024**3), "data_limit": int(50 * 1024**3),
    "expire": None, "status": "active",
    "subscription_url": "https://sub.example.com/s/def456",
}

ACCOUNT_LINKED_ACTIVE = {
    "account_id": 110, "service_name": None, "marzban_username": "seyed_family_2",
    "source": "linked", "used_traffic": int(3.2 * 1024**3), "data_limit": int(20 * 1024**3),
    "expire": 1893456000, "status": "active",
    "subscription_url": "https://sub.example.com/s/lk890",
}


class Backend:
    """Configurable per persona; journeys set what they need."""

    def __init__(self, session: dict, accounts: list | None = None,
                 wallet: list | None = None, order: dict | None = None):
        self.session = session
        self.accounts = accounts or []
        self.wallet = wallet or []
        self.order = order
        self.debt_paid = False

    async def get(self, path, params=None):
        if path == "/api/shop/bot/wallet":
            return self.wallet
        if path == "/api/shop/bot/accounts":
            return self.accounts
        raise AssertionError(path)

    async def post(self, path, json=None, timeout: float = 20):
        if path == "/api/shop/bot/session":
            return dict(self.session)
        if path == "/api/shop/bot/orders":
            return dict(self.order)
        if path == "/api/shop/bot/trial":
            return {"ok": True}
        if path == "/api/shop/bot/topups":
            return {"reference_code": "K7PD", "status": "pending"}
        if path == "/api/shop/bot/orders/77/pay" or path == "/api/shop/bot/orders/78/pay":
            # The wallet buy really debits: the SESSION read after this call
            # reflects the new balance, so a later screen shows real numbers.
            self.session["balance"] = 50_000
            return {"ok": True}
        if path == "/api/shop/bot/pay-debt-from-wallet":
            # First call pays min(wallet, debt); the session then reflects
            # reality, so a SECOND tap of the same (now stale) button finds
            # an empty wallet and refuses — exactly the live behaviour.
            if not self.debt_paid:
                self.debt_paid = True
                self.session["balance"] = 0
                self.session["ledger_debt"] = 65_000.0
                return {"paid": 120_000, "wallet_balance": 0, "ledger_debt": 65_000}
            raise ShopApiError(
                "موجودی‌تان خالی است — برای پرداختِ مانده، رسید کارت‌به‌کارت بفرستید.", 400)
        raise AssertionError(path)


# ── recorder ──────────────────────────────────────────────────────────────

def _keyboard_rows(markup) -> list[tuple[str, str]]:
    if markup is None:
        return []
    out: list[tuple[str, str]] = []
    inline = getattr(markup, "inline_keyboard", None)
    if inline is not None:
        for row in inline:
            for b in row:
                url = getattr(b, "url", None)
                out.append((b.text, f"URL: {url}" if url else f"callback: `{b.callback_data}`"))
        return out
    kb = getattr(markup, "keyboard", None)
    if kb is not None:
        for row in kb:
            for b in row:
                contact = getattr(b, "request_contact", False)
                out.append((b.text, "درخواست شمارهٔ تلفن" if contact else "دکمهٔ منو (متن)"))
    return out


class Transcript:
    """One persona's journey: ordered (tap, messages) entries."""

    def __init__(self, persona: str, session: dict, accounts=None, wallet=None,
                 order=None):
        self.persona = persona
        self.backend = Backend(dict(session), accounts, wallet, order)
        shop.backend = self.backend
        self.entries: list[tuple[str, list[tuple[str, object]]]] = []
        self.context = Context()

    def _update(self, text=None, cb=None):
        msg = Msg(text)
        upd = SimpleNamespace(
            effective_message=msg, message=msg,
            effective_user=SimpleNamespace(id=555, username="ali_t",
                                           first_name="علی", last_name=None),
        )
        if cb is not None:
            upd.callback_query = Query(cb, msg)
        return upd, msg

    async def step(self, label: str, *, text: str | None = None, cb: str | None = None,
                   handler=None, fresh: bool = False, count: bool = True):
        """One customer action → record every message the bot sends back."""
        if fresh:
            self.context = Context()
        upd, msg = self._update(text, cb)
        if handler is None:
            handler = shop.handle_text
        await handler(upd, self.context)
        self.entries.append((label, msg.sent, count))
        return msg

    def start_screen(self, label: str, upd) -> None:
        """Record a journey opener (e.g. /start) without a tap number."""
        self.entries.append((label, upd.effective_message.sent, False))

    def render(self, out: list[str]) -> None:
        out.append(f"\n## {self.persona}\n")
        tap = 0
        for label, sent, counted in self.entries:
            if counted:
                tap += 1
                out.append(f"### تپ {tap} — {label}\n")
            else:
                out.append(f"### {label}\n")
            for text, markup in sent:
                out.append("```\n" + text + "\n```")
                rows = _keyboard_rows(markup)
                if rows:
                    out.append("\n| دکمه | اثر |")
                    out.append("|---|---|")
                    for btn, target in rows:
                        out.append(f"| `{btn}` | {target} |")
                    out.append("")
                else:
                    out.append("\n(بدون دکمه)\n")


# ── personas ──────────────────────────────────────────────────────────────

ORDER_CARD = {  # card-to-card intent (wallet empty) — ۳۵ گیگِ تایپ‌شدهٔ مشتری
    "order_id": 77, "creates_new": False, "data_limit_gb": 35,
    "duration_days": 30, "price": 350_000, "balance": 0,
    "shortfall": 350_000, "payable_from_wallet": False,
    "card_number": "6037-9911-2222-3333", "card_holder": None,
    "approval_eta_minutes": 30,
}
ORDER_WALLET = {  # wallet covers it
    "order_id": 78, "creates_new": False, "data_limit_gb": 30,
    "duration_days": 30, "price": 300_000, "balance": 350_000,
    "shortfall": 0, "payable_from_wallet": True,
    "card_number": "6037-9911-2222-3333", "card_holder": None,
    "approval_eta_minutes": 30,
}


async def persona_new(out: list[str]) -> None:
    s = dict(SESSION_BASE, trial_enabled=True, trial_available=True)
    t = Transcript("پرسونا ۱ — تازه‌وارد (تستِ رایگان در دسترس)", s, order=ORDER_CARD)
    upd, _ = t._update()
    upd.callback_query = None
    await shop.start(upd, t.context)
    t.entries.append(("/start (ورود اول)", upd.effective_message.sent, False))
    await t.step("تپ «🎁 تست رایگان»", text=texts.MENU_TRIAL, handler=shop.take_trial, fresh=True)
    await t.step("تپ «🛒 خرید سرویس»", text=texts.MENU_BUY, handler=shop.start_buy, fresh=True)
    await t.step("تپ «✏️ حجم دلخواه»", cb="vol:custom", handler=shop.on_volume_choice)
    await t.step("تایپ «۳۵» (حجم) → صفحهٔ پرداختِ کارت‌به‌کارت", text="۳۵")
    await t.step("تایپ «سلام» وسط انتظارِ رسید", text="سلام")
    await t.step("تپ «❌ انصراف»", text=texts.CANCEL)
    await t.step("تپ «❓ راهنما و پشتیبانی»", text=texts.MENU_HELP_SUPPORT, fresh=True)
    await t.step("تایپ «پنج‌شنبه خوبی؟» (متنِ بی‌مسیر)", text="پنج‌شنبه خوبی؟")
    await t.step("دکمهٔ inlineِ کهنهٔ «تأیید و دریافت» (بعد از ری‌استارت)",
                 cb="buy:confirm", handler=shop.on_buy_confirm, fresh=True)
    t.render(out)


async def persona_one_service(out: list[str]) -> None:
    s = dict(SESSION_BASE, has_accounts=True, balance=350_000)
    t = Transcript("پرسونا ۲ — یک سرویسِ فعال + موجودی (خریدِ کیف‌پولی، شارژ، کارت)",
                   s, accounts=[dict(ACCOUNT_ACTIVE)],
                   wallet=[{"type": "topup", "amount": 300_000, "note": "واریز"},
                           {"type": "purchase", "amount": -300_000, "note": "خرید"}],
                   order=ORDER_WALLET)
    upd, _ = t._update()
    upd.callback_query = None
    await shop.start(upd, t.context)
    t.entries.append(("/start (ورود)", upd.effective_message.sent, False))
    await t.step("تپ «🛒 خرید / تمدید سرویس»", text=texts.MENU_BUY_RENEW, handler=shop.start_buy, fresh=True)
    await t.step("تپ «۳۰ گیگ»", cb="vol:30", handler=shop.on_volume_choice)
    await t.step("تپ «✅ تأیید و دریافت» — خرید با موجودی، یک تپ",
                 cb="buy:confirm", handler=shop.on_buy_confirm)
    t.entries.append((
        "(توضیح) بلافاصله بعدش، بک‌اند تحویل را می‌فرستد (پوش از سمت سرور، نه جوابِ این تپ): "
        "یک پیام — عکسِ QR با کپشنِ (خط پلن + لینک در یک خطِ مستقل + راهنمای اتصال) و دکمهٔ URL «🔗 لینک اشتراک».",
        [], False))
    await t.step("تپ همان دکمهٔ (الان کهنهٔ) تأیید دوباره", cb="buy:confirm", handler=shop.on_buy_confirm)
    await t.step("تپ «📱 سرویس‌های من»", text=texts.MENU_ACCOUNTS, handler=shop.show_accounts, fresh=True)
    await t.step("تپ «⋯ بیشتر» روی کارت", cb=f"svcmore:{ACCOUNT_ACTIVE['account_id']}",
                 handler=shop.on_service_more)
    t.backend.order = {**ORDER_WALLET, "order_id": 79, "balance": 50_000,
                       "payable_from_wallet": False, "shortfall": 250_000}
    await t.step("تپ «🔄 تمدید» روی کارت — با موجودیِ ۵۰٬۰۰۰ِ پس‌مانده (کارت‌به‌کارت)",
                 cb="renew:30", handler=shop.on_renew, fresh=True)
    await t.step("تپ «💳 حساب من»", text=texts.MENU_ACCOUNT, handler=shop.show_account, fresh=True)
    await t.step("تپ «➕ شارژ» (inline)", cb="acct:topup", handler=shop.on_account_topup)
    await t.step("تایپ «۲۰۰ هزار»", text="۲۰۰ هزار")
    await t.step("تایپ «چقدر طول می‌کشه؟» (سؤال، بیرونِ هر فلو)", text="چقدر طول می‌کشه؟", fresh=True)
    t.render(out)


async def persona_two_services(out: list[str]) -> None:
    s = dict(SESSION_BASE, has_accounts=True)
    accs = [dict(ACCOUNT_ACTIVE, renews_here=False),
            dict(ACCOUNT_ENDED, renews_here=True),
            dict(ACCOUNT_LINKED_ACTIVE, renews_here=False)]
    t = Transcript("پرسونا ۳ — دو سرویس (یکی تمام‌شده) + یک سرویسِ وصل‌شده", s, accounts=accs)
    upd, _ = t._update()
    upd.callback_query = None
    await shop.start(upd, t.context)
    t.entries.append(("/start (ورود)", upd.effective_message.sent, False))
    await t.step("تپ «📱 سرویس‌های من» — سه کارت، تمدید فقط روی کارتی که واقعاً تمدید می‌شود",
                 text=texts.MENU_ACCOUNTS, handler=shop.show_accounts, fresh=True)
    await t.step("دکمهٔ «⋯ بیشتر» با دادهٔ بدشکل (کهنه)", cb="svcmore:",
                 handler=shop.on_service_more)
    t.render(out)


async def persona_debt(out: list[str]) -> None:
    s = dict(SESSION_BASE, has_accounts=True, balance=0, ledger_debt=185_000.0, debt_exact=False,
             debt_breakdown=[{"name": "سعید", "debt": 120_000, "exact": True},
                             {"name": "حساب برادر", "debt": 65_000, "exact": False}])
    t = Transcript("پرسونا ۴ — بدهکار، کیفِ خالی (مسیرِ رسید + جزئیات)", s,
                   accounts=[dict(ACCOUNT_ACTIVE)])
    upd, _ = t._update()
    upd.callback_query = None
    await shop.start(upd, t.context)
    t.entries.append(("/start (ورود)", upd.effective_message.sent, False))
    await t.step("تپ «💳 ماندهٔ پرداخت: ۱۸۵,۰۰۰ تومان» (برچسبِ دکمهٔ منو)",
                 text=texts.menu_account_label(185_000), handler=shop.show_account, fresh=True)
    await t.step("تپ «جزئیات»", cb="acct:detail", handler=shop.on_account_detail)
    await t.step("تپ «بازگشت»", cb="acct:back", handler=shop.on_account_back)
    await t.step("تپ «ارسال رسید کارت‌به‌کارت»", cb="debt:receipt", handler=shop.on_pay_debt, fresh=True)
    await t.step("تپ «همین مبلغ»", text=texts.DEBT_AMOUNT_AS_IS)
    await t.step("تایپ کدِ پیگیریِ تراکنش «3421 5678 90»", text="3421 5678 90")
    await t.step("تپ «❌ انصراف» (بعد از ثبتِ رسید؛ کاری در جریان نیست)",
                 text=texts.CANCEL, fresh=True)
    await t.step("تایپ «بدهیم چنده؟» (واژهٔ پول در متنِ آزاد)", text="بدهیم چنده؟", fresh=True)
    t.render(out)


async def persona_debt_with_balance(out: list[str]) -> None:
    s = dict(SESSION_BASE, has_accounts=True, ledger_debt=185_000.0, balance=120_000)
    t = Transcript("پرسونا ۵ — بدهکار با موجودی (پرداختِ یک‌تپی از موجودی)", s,
                   accounts=[dict(ACCOUNT_ACTIVE)])
    upd, _ = t._update()
    upd.callback_query = None
    await shop.start(upd, t.context)
    t.entries.append(("/start (ورود)", upd.effective_message.sent, False))
    await t.step("تپ «💳 ماندهٔ پرداخت: …»", text=texts.menu_account_label(185_000),
                 handler=shop.show_account, fresh=True)
    await t.step("تپ «💳 پرداخت از موجودی (۱۲۰٬۰۰۰ تومان)» — یک تپ، پرداخت انجام می‌شود",
                 cb="debt:wallet", handler=shop.on_pay_debt)
    await t.step("تپ همان دکمهٔ (الان کهنهٔ) پرداخت، دوباره", cb="debt:wallet", handler=shop.on_pay_debt)
    t.render(out)


async def main() -> None:
    out: list[str] = [
        "# Shop bot — screen transcript (Round 2 · R3)",
        "",
        "<div dir=\"rtl\">",
        "",
        "تولیدشده توسط `shopbot/dump_screens.py` (2026-11-06) — هندلرهای واقعیِ `handlers/shop.py` با تلگرام/بک‌اندِ قلابی پیموده شده‌اند؛ هر متن عیناً خروجیِ ربات است. «دکمهٔ منو (متن)» = دکمهٔ reply-keyboard پایینِ صفحه (منوی اصلی همیشه زیرِ صفحه است)، «callback: …» = دکمهٔ inline روی خودِ پیام، «URL: …» = دکمهٔ لینک.",
        "",
        "(بدون دکمه) یعنی پیامِ جدید هیچ دکمهٔ inline ندارد؛ کیبوردِ reply (منوی اصلی) قبلی زیرِ صفحه پابرجا می‌ماند — تلگرام کیبورد را تا پیامِ بعدیِ دارای کیبورد عوض نمی‌کند.",
        "",
        "پوشش: منو، سرویس‌های من، تمدید، خرید (صفحهٔ حجم → خلاصه → تأیید؛ دو واریانتِ کیف‌پولی و کارت‌به‌کارت)، حساب من + جزئیات، پرداخت بدهی (موجودی/رسید/انصراف)، شارژ، تست رایگان، راهنما/پشتیبانی، متنِ بی‌مسیر، دکمهٔ کهنه.",
        "</div>",
    ]
    await persona_new(out)
    await persona_one_service(out)
    await persona_two_services(out)
    await persona_debt(out)
    await persona_debt_with_balance(out)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    print(f"wrote {OUT}")


asyncio.run(main())
