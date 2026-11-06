"""T2 menu-rebuild regression tests (feat/shopbot-ux-rebuild, docs/ZCODE_SHOPBOT_UX_DESIGN.md):

  * Layout A — the main menu is at most 4 rows: trial (when available),
    buy | my-services, ONE account door, help & support. The old
    wallet/top-up/pay-debt/new-service/payg top-level rows are gone from the
    NEW keyboard.
  * «حساب من» shows ONE number: the debt (with the pay action) when the
    customer owes, the balance (with the top-up action) otherwise — never
    both side by side.
  * One-tap debt payment from the wallet: the button carries the amount, the
    tap pays; the old debt:wallet:yes confirm button still pays (old chats).
  * The buy screen carries «سرویس جداگانه» and «سرویس مصرفی» as CONDITIONAL
    buttons (has_accounts / payg_enabled and no payg service yet).
  * Every old menu label a chat may still show keeps routing to its action.

Telegram objects and the backend client are stubbed — nothing real is
touched. Same script style as test_prepublic_p0p1_bot.py.

Run (from shopbot/):
  ../bot/venv/Scripts/python test_menu_rebuild.py
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
            data=cb_data,
            answer=_answer,
            edit_message_text=msg.edit_message_text,
            message=msg,
        )
    context = SimpleNamespace(args=[], user_data={}, chat_data={})
    return (upd, context), msg


def rows_of(markup) -> list[list[str]]:
    if markup is None:
        return []
    if hasattr(markup, "inline_keyboard"):  # inline keyboard (T4 buy screen)
        return [[btn.text for btn in row] for row in markup.inline_keyboard]
    return [[btn.text for btn in row] for row in markup.keyboard]


def flat(markup) -> list[str]:
    return [t for row in rows_of(markup) for t in row]


def base_session(**over) -> dict:
    s = {
        "balance": 0, "trial_available": False, "shop_name": None, "support_handle": None,
        "is_blocked": False, "is_open": True,
        "min_topup": 50_000, "max_topup": 5_000_000, "card_number": "6037-9911-2222-3333",
        "card_holder": None, "approval_eta_minutes": 30,
        "price_per_gb": 10_000, "min_gb": 5, "max_gb": 100, "plan_duration_days": 30,
        "ledger_debt": 0, "debt_breakdown": [], "debt_exact": True,
        "payg_enabled": False, "payg_rate_per_gb": 0,
        "has_accounts": False, "has_payg_service": False,
    }
    s.update(over)
    return s


SESSION = base_session()
HISTORY: list[dict] = []
PAY_DEBT_POSTS: list[dict] = []


async def fake_session(update) -> dict:
    return dict(SESSION)


class StubBackend:
    async def get(self, path, params=None):
        if path == "/api/shop/bot/wallet":
            return list(HISTORY)
        raise AssertionError(path)

    async def post(self, path, json=None, timeout: float = 20):
        if path == "/api/shop/bot/pay-debt-from-wallet":
            PAY_DEBT_POSTS.append(json or {})
            return {"paid": SESSION["balance"], "wallet_balance": 0, "ledger_debt": 0}
        raise AssertionError(path)


stub = StubBackend()
shop.backend = stub
shop._session = fake_session  # type: ignore[assignment]


async def main() -> None:
    # ── 1. newcomer with a trial sees 3 rows, nothing else ───────────────
    SESSION.update(base_session(trial_available=True))
    (upd, context), msg = make_update()
    await shop.start(upd, context)
    markup = msg.replies[-1][1]
    check("trial newcomer menu = [تست رایگان] / [خرید] / [راهنما و پشتیبانی]",
          rows_of(markup) == [
              [texts.MENU_TRIAL],
              [texts.MENU_BUY],
              [texts.MENU_HELP_SUPPORT],
          ], repr(rows_of(markup)))
    check("trial newcomer has no سرویس‌های من / حساب من / کیف پول / ساخت جدید / مصرفی",
          not any(t in flat(markup) for t in (
              texts.MENU_ACCOUNTS, texts.MENU_ACCOUNT, texts.MENU_WALLET,
              texts.MENU_TOPUP, texts.MENU_PAY_DEBT, texts.MENU_NEW_SERVICE, texts.MENU_PAYG)))

    # ── 2. returning customer: buy|services / حساب من / راهنما ──────────
    SESSION.update(base_session(has_accounts=True, balance=80_000))
    (upd, context), msg = make_update()
    await shop.start(upd, context)
    markup = msg.replies[-1][1]
    check("returning menu = [خرید/تمدید | سرویس‌های من] / [حساب من] / [راهنما و پشتیبانی]",
          rows_of(markup) == [
              [texts.MENU_BUY_RENEW, texts.MENU_ACCOUNTS],
              [texts.MENU_ACCOUNT],
              [texts.MENU_HELP_SUPPORT],
          ], repr(rows_of(markup)))
    check("returning menu has NO تست رایگان / ساخت جدید / مصرفی / کیف پول / شارژ / پرداخت بدهی",
          not any(t in flat(markup) for t in (
              texts.MENU_TRIAL, texts.MENU_NEW_SERVICE, texts.MENU_PAYG,
              texts.MENU_WALLET, texts.MENU_TOPUP, texts.MENU_PAY_DEBT)))

    # ── 3. debtor: the account button IS the debt number ─────────────────
    SESSION.update(base_session(has_accounts=True, balance=30_000, ledger_debt=123_000))
    (upd, context), msg = make_update()
    await shop.start(upd, context)
    markup = msg.replies[-1][1]
    debt_btn = [t for t in flat(markup) if "پرداخت" in t and texts.MENU_PAY_DEBT != t]
    check("debtor menu carries a «ماندهٔ پرداخت» button with the amount",
          any("ماندهٔ پرداخت" in t and "۱۲۳,۰۰۰" in t for t in flat(markup)),
          repr(flat(markup)))
    check("debtor menu has no plain «حساب من» button",
          texts.MENU_ACCOUNT not in flat(markup))

    # ── 4. main_menu(None) still renders a working fallback ──────────────
    markup = shop.main_menu(None)
    check("no-session menu = [خرید | سرویس‌های من] / [راهنما و پشتیبانی]",
          rows_of(markup) == [
              [texts.MENU_BUY, texts.MENU_ACCOUNTS],
              [texts.MENU_HELP_SUPPORT],
          ], repr(rows_of(markup)))

    # ── 5. every button the menu shows is dispatchable ───────────────────
    all_labels: set[str] = set()
    for sess in (base_session(trial_available=True), base_session(has_accounts=True, balance=1),
                 base_session(has_accounts=True, ledger_debt=500), base_session(),
                 None):
        all_labels.update(flat(shop.main_menu(sess)))
    all_labels.update({texts.NEW_SERVICE_ON_BUY, texts.PAYG_ON_BUY})
    check("every menu/buy-screen button label routes (_is_menu_tap)",
          all(shop._is_menu_tap(t) for t in all_labels),
          repr([t for t in all_labels if not shop._is_menu_tap(t)]))

    # ── 6-9. old labels still route; new labels route ────────────────────
    SESSION.update(base_session(has_accounts=True, balance=80_000))

    # old «💰 کیف پول» → the merged account view (wallet flavour)
    (upd, context), msg = make_update(shop.texts.MENU_WALLET)
    await shop.handle_text(upd, context)
    buttons = [(b.text, b.callback_data) for row in msg.replies[-1][1].inline_keyboard for b in row]
    check("old «کیف پول» label → wallet flavour with the شارژ button",
          "موجودی" in msg.replies[-1][0]
          and any(c == "acct:topup" for _, c in buttons),
          repr((msg.replies[-1][0], buttons)))

    # old «➕ شارژ کیف پول» → the top-up amount ask (state set)
    (upd, context), msg = make_update(shop.texts.MENU_TOPUP)
    await shop.handle_text(upd, context)
    check("old «شارژ کیف پول» label → top-up amount flow",
          context.user_data.get(shop._STATE) == shop._STATE_CHOOSING_TOPUP)

    # old «💳 پرداخت بدهی صورت‌حساب» → debt view with the pay buttons
    SESSION.update(base_session(has_accounts=True, balance=30_000, ledger_debt=123_000,
                                debt_breakdown=[{"name": "سید", "debt": 123000}]))
    (upd, context), msg = make_update(shop.texts.MENU_PAY_DEBT)
    await shop.handle_text(upd, context)
    markup = msg.replies[-1][1]
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    check("old «پرداخت بدهی» label → debt view with wallet-pay + receipt buttons",
          "debt:wallet" in callbacks and "debt:receipt" in callbacks, repr(callbacks))
    texts_shown = msg.replies[-1][0]
    check("debt view shows the debt number and NOT the wallet balance beside it",
          "۱۲۳,۰۰۰" in texts_shown and "۳۰,۰۰۰" not in texts_shown, repr(texts_shown))

    # new «💳 حساب من» with a positive balance → wallet flavour
    SESSION.update(base_session(has_accounts=True, balance=80_000))
    (upd, context), msg = make_update(texts.MENU_ACCOUNT)
    await shop.handle_text(upd, context)
    buttons = [(b.text, b.callback_data) for row in msg.replies[-1][1].inline_keyboard for b in row]
    check("new «حساب من» label (positive balance) → wallet flavour with the شارژ button",
          "موجودی" in msg.replies[-1][0] and any(c == "acct:topup" for _, c in buttons))

    # ── 10. merged help & support from all three labels ──────────────────
    SESSION.update(base_session(has_accounts=True, support_handle="vpn_support"))
    for label in (texts.MENU_HELP_SUPPORT, texts.MENU_HELP, texts.MENU_SUPPORT):
        (upd, context), msg = make_update(label)
        await shop.handle_text(upd, context)
        check(f"«{label[:12]}…» → merged help with the support handle",
              "@vpn_support" in msg.replies[-1][0], repr(msg.replies[-1][0]))

    # ── 11. the new buy label opens the (non-forced) buy screen ─────────
    SESSION.update(base_session(has_accounts=True))
    (upd, context), msg = make_update(texts.MENU_BUY_RENEW)
    await shop.handle_text(upd, context)
    check("«خرید / تمدید» → volume screen WITHOUT the separate-service intro",
          texts.NEW_SERVICE_INTRO not in msg.replies[-1][0])

    # ── 12-14. debt view internals ───────────────────────────────────────
    SESSION.update(base_session(has_accounts=True, balance=30_000, ledger_debt=123_000,
                                debt_breakdown=[]))
    (upd, context), msg = make_update(texts.MENU_ACCOUNT)
    await shop.handle_text(upd, context)
    markup = msg.replies[-1][1]
    buttons = [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]
    wallet_btn = next((t for t, c in buttons if c == "debt:wallet"), None)
    check("wallet-pay button carries min(balance, debt) = ۳۰,۰۰۰",
          wallet_btn is not None and "۳۰,۰۰۰" in wallet_btn, repr(buttons))
    check("no جزئیات button when the backend sent no breakdown",
          not any(c == "acct:detail" for _, c in buttons), repr(buttons))

    SESSION["debt_breakdown"] = [{"name": "سید", "debt": 123000}]
    (upd, context), msg = make_update(texts.MENU_ACCOUNT)
    await shop.handle_text(upd, context)
    buttons = [(b.text, b.callback_data) for row in msg.replies[-1][1].inline_keyboard for b in row]
    check("جزئیات button appears when a breakdown exists",
          any(c == "acct:detail" for _, c in buttons), repr(buttons))

    SESSION["balance"] = 0
    (upd, context), msg = make_update(texts.MENU_ACCOUNT)
    await shop.handle_text(upd, context)
    buttons = [(b.text, b.callback_data) for row in msg.replies[-1][1].inline_keyboard for b in row]
    check("empty wallet → NO wallet-pay button; receipt is primary",
          not any(c == "debt:wallet" for _, c in buttons)
          and any(c == "debt:receipt" for _, c in buttons), repr(buttons))

    # ── 15-16. one-tap wallet payment; old confirm still pays ────────────
    SESSION.update(base_session(has_accounts=True, balance=30_000, ledger_debt=30_000))
    PAY_DEBT_POSTS.clear()
    (upd, context), msg = make_update(cb_data="debt:wallet")
    await shop.on_pay_debt(upd, context)
    check("debt:wallet pays IMMEDIATELY (one tap)",
          len(PAY_DEBT_POSTS) == 1, repr(PAY_DEBT_POSTS))
    check("final text is the paid summary",
          "پرداخت شد" in msg.replies[-1][0] or texts.DEBT_NONE in msg.replies[-1][0]
          or "بدهی شما کامل پرداخت شد" in msg.replies[-1][0], repr(msg.replies[-1][0]))

    (upd, context), msg = make_update(cb_data="debt:wallet:yes")
    await shop.on_pay_debt_wallet_confirm(upd, context)
    check("old debt:wallet:yes confirm still pays (old chats)",
          len(PAY_DEBT_POSTS) == 2)

    # ── 17. wallet view keeps at most 3 transaction lines ────────────────
    SESSION.update(base_session(has_accounts=True, balance=80_000))
    HISTORY.clear()
    HISTORY.extend([{"type": "topup", "amount": 100_000, "note": None}] * 9)
    (upd, context), msg = make_update(texts.MENU_ACCOUNT)
    await shop.handle_text(upd, context)
    body = msg.replies[-1][0]
    check("account view lists at most 3 recent transactions",
          body.count("شارژ کیف پول") <= 3, repr(body))

    # ── 18-20. conditional buttons on the buy screen ─────────────────────
    SESSION.update(base_session(has_accounts=True, payg_enabled=True, has_payg_service=False))
    (upd, context), msg = make_update(texts.MENU_BUY)
    await shop.handle_text(upd, context)
    markup = msg.replies[-1][1]
    check("buy screen carries BOTH سرویس جداگانه and سرویس مصرفی when relevant",
          texts.NEW_SERVICE_ON_BUY in flat(markup) and texts.PAYG_ON_BUY in flat(markup),
          repr(flat(markup)))

    SESSION.update(base_session(has_accounts=False, payg_enabled=True, has_payg_service=False))
    (upd, context), msg = make_update(texts.MENU_BUY)
    await shop.handle_text(upd, context)
    markup = msg.replies[-1][1]
    check("no سرویس جداگانه button for a customer with no services",
          texts.NEW_SERVICE_ON_BUY not in flat(markup))

    SESSION.update(base_session(has_accounts=True, payg_enabled=True, has_payg_service=True))
    (upd, context), msg = make_update(texts.MENU_BUY)
    await shop.handle_text(upd, context)
    markup = msg.replies[-1][1]
    check("no سرویس مصرفی button when the customer already has one",
          texts.PAYG_ON_BUY not in flat(markup), repr(flat(markup)))

    SESSION.update(base_session(has_accounts=True, payg_enabled=False))
    (upd, context), msg = make_update(texts.MENU_BUY)
    await shop.handle_text(upd, context)
    check("no سرویس مصرفی button when payg is off",
          texts.PAYG_ON_BUY not in flat(msg.replies[-1][1]))

    # the two new buy-screen buttons route
    SESSION.update(base_session(has_accounts=True, payg_enabled=True))
    (upd, context), msg = make_update(texts.NEW_SERVICE_ON_BUY)
    await shop.handle_text(upd, context)
    check("«سرویس جداگانه» on the buy screen → forced-new volume screen",
          texts.NEW_SERVICE_INTRO in msg.replies[-1][0]
          and context.user_data.get(shop._FORCE_NEW_SERVICE) is True)
    (upd, context), msg = make_update(texts.PAYG_ON_BUY)
    await shop.handle_text(upd, context)
    check("«سرویس مصرفی» on the buy screen → payg intro flow",
          context.user_data.get(shop._STATE) == shop._STATE_AWAITING_PAYG_USERNAME)

    # ── 21. «همین مبلغ» pays the whole debt by receipt ───────────────────
    SESSION.update(base_session(has_accounts=True, balance=0, ledger_debt=123_000))
    (upd, context), msg = make_update(texts.MENU_ACCOUNT)
    await shop.handle_text(upd, context)          # debt view
    buttons = [(b.text, b.callback_data) for row in msg.replies[-1][1].inline_keyboard for b in row]
    check("receipt button present for the no-wallet debtor", any(c == "debt:receipt" for _, c in buttons))
    (upd, context), msg = make_update(cb_data="debt:receipt")
    await shop.on_pay_debt(upd, context)
    check("debt:receipt opens the amount ask with «همین مبلغ» first",
          msg.replies and any(
              r[1] is not None and hasattr(r[1], "keyboard")
              and texts.DEBT_AMOUNT_AS_IS in [b.text for row in r[1].keyboard for b in row]
              for r in msg.replies),
          repr(msg.replies))
    # Same context: the amount ask's state lives there — «همین مبلغ» now
    # submits the WHOLE debt without typing a number.
    msg.text = texts.DEBT_AMOUNT_AS_IS
    await shop.handle_text(upd, context)
    check("«همین مبلغ» submits the FULL debt into the receipt flow",
          context.user_data.get(shop._PENDING_AMOUNT) == 123_000
          and context.user_data.get(shop._PENDING_FOR_DEBT) is True,
          repr(context.user_data))


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All shopbot menu-rebuild cases passed.")
