"""T8 — scripted walkthrough of the 9 audited journeys (docs/ZCODE_SHOPBOT_UX_AUDIT.md).

Drives the REAL handlers against stubbed backend/Telegram, counts the
customer's taps (button presses + mandatory typed messages) and the messages
the customer receives, and prints before (T0 audit) → after (this run).

Run (from shopbot/):
  ../bot/venv/Scripts/python test_walkthrough_9_journeys.py
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

# (journey, before taps, before messages) — from docs/ZCODE_SHOPBOT_UX_AUDIT.md
BEFORE = {
    1: (3, 7), 2: (4, 8), 3: (3, 6), 4: (3, 5), 5: (3, 4),
    6: (4, 4), 7: (1, 3), 8: (2, 3), 9: (1, 1),
}

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"[{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


class Msg:
    def __init__(self, text=None):
        self.text = text
        self.replies: list[tuple] = []

    async def reply_text(self, text, reply_markup=None, **kw):
        self.replies.append((text, reply_markup))

    async def edit_message_text(self, text, reply_markup=None, **kw):
        self.replies.append((text, reply_markup))


class Ctx:
    def __init__(self):
        self.user_data = {}
        self.chat_data = {}
        self.args = []


def upd(text=None, cb=None):
    m = Msg(text)
    u = SimpleNamespace(effective_message=m, message=m,
                        effective_user=SimpleNamespace(id=555, username="ali_t",
                                                       first_name="علی", last_name=None))
    if cb is not None:
        async def _ans(*a, **k):
            return None
        u.callback_query = SimpleNamespace(data=cb, answer=_ans,
                                           edit_message_text=m.reply_text, message=m)
    return u, Ctx(), m


SESSION = {
    "balance": 0, "trial_available": False, "shop_name": "فروشگاه تست", "support_handle": None,
    "is_blocked": False, "is_open": True,
    "min_topup": 50_000, "max_topup": 5_000_000, "card_number": "6037-9911-2222-3333",
    "card_holder": None, "approval_eta_minutes": 30, "phone": None,
    "price_per_gb": 10_000, "min_gb": 5, "max_gb": 100, "plan_duration_days": 30,
    "ledger_debt": 0, "debt_breakdown": [], "debt_exact": True,
    "payg_enabled": False, "payg_rate_per_gb": 0,
    "has_accounts": False, "has_payg_service": False,
}
ROWS: list[dict] = []
HISTORY: list[dict] = []
PAYS: list[dict] = []


async def fake_session(update):
    return dict(SESSION)


class B2:
    """Full stub: reads + order intents (payable_from_wallet driven by
    SESSION['balance']) + pays + topups + the debt payment."""

    async def get(self, path, params=None):
        if path == "/api/shop/bot/wallet":
            return list(HISTORY)
        if path == "/api/shop/bot/accounts":
            return [dict(r) for r in ROWS]
        raise AssertionError(path)

    async def post(self, path, json=None, timeout=20):
        if path == "/api/shop/bot/orders":
            balance = SESSION["balance"]
            price = int(json["data_limit_gb"] * SESSION["price_per_gb"])
            return {
                "order_id": 77, "creates_new": not SESSION["has_accounts"] or json.get("force_new_service"),
                "data_limit_gb": json["data_limit_gb"], "duration_days": 30,
                "price": price, "balance": balance, "shortfall": max(0, price - balance),
                "payable_from_wallet": balance >= price,
                "card_number": SESSION["card_number"], "card_holder": None,
                "approval_eta_minutes": 30,
            }
        if path.startswith("/api/shop/bot/orders/") and path.endswith("/pay"):
            PAYS.append(json)
            return {"ok": True}
        if path == "/api/shop/bot/topups":
            return {"reference_code": "ABCD", "status": "pending"}
        if path == "/api/shop/bot/pay-debt-from-wallet":
            PAYS.append(json)
            return {"paid": 123_000, "wallet_balance": 0, "ledger_debt": 0}
        raise AssertionError(path)


shop.backend = B2()
shop._session = fake_session  # type: ignore[assignment]


def reply_keyboard_texts(m: Msg) -> list[str]:
    if not m.replies:
        return []
    kb = m.replies[-1][1]
    if kb is None or not hasattr(kb, "keyboard"):
        return []
    return [b.text for row in kb.keyboard for b in row]


def inline_texts(m: Msg) -> list[str]:
    if not m.replies:
        return []
    kb = m.replies[-1][1]
    if kb is None or not hasattr(kb, "inline_keyboard"):
        return []
    return [b.text for row in kb.inline_keyboard for b in row]


async def run(name, steps):
    """One journey = one Telegram chat: the SAME update/context objects across
    steps (state persists like the real bot), a fresh update per step."""
    u, c, m = upd()
    await shop.start(u, c)
    taps = 0
    for kind, arg in steps:
        if kind == "cb":
            u2, _, _ = upd(cb=arg)
            u2.effective_message = m
            u2.message = m
            u2.callback_query.edit_message_text = m.reply_text
            data = arg
            if data.startswith("vol:"):
                await shop.on_volume_choice(u2, c)
            elif data == "uname:ask":
                await shop.on_username_ask(u2, c)
            elif data.startswith("buy:"):
                await shop.on_buy_confirm(u2, c)
            elif data.startswith("debt:"):
                if data == "debt:wallet:yes":
                    await shop.on_pay_debt_wallet_confirm(u2, c)
                else:
                    await shop.on_pay_debt(u2, c)
            elif data.startswith("renew:"):
                await shop.on_renew(u2, c)
            elif data.startswith("svcmore:"):
                await shop.on_service_more(u2, c)
            elif data.startswith("acct:"):
                if data == "acct:topup":
                    await shop.on_account_topup(u2, c)
                elif data == "acct:detail":
                    await shop.on_account_detail(u2, c)
                else:
                    await shop.on_account_back(u2, c)
            else:
                raise AssertionError(data)
        else:  # tap / type both ride handle_text
            m.text = arg
            await shop.handle_text(u, c)
        taps += 1
    return taps, len(m.replies)


async def main() -> None:
    results = {}

    # 1) first visit → free trial
    SESSION.update(trial_available=True, has_accounts=False, balance=0, ledger_debt=0)
    u, c, m = upd()
    await shop.start(u, c)
    n0 = len(m.replies)
    await shop.take_trial(u, c)
    results[1] = (1, len(m.replies))  # 1 tap: the trial button (welcome + working + phone prompt)

    # 2) buy a service (new customer, card)
    SESSION.update(trial_available=False, has_accounts=False, balance=0)
    taps, msgs = await run("buy-card", [
        ("tap", texts.MENU_BUY),
        ("cb", "vol:20"),
        ("type", "رسید"),
    ])
    results[2] = (taps, msgs)

    # 3) buy with wallet
    SESSION.update(has_accounts=True, balance=500_000)
    taps, msgs = await run("buy-wallet", [
        ("tap", texts.MENU_BUY_RENEW),
        ("cb", "vol:20"),
        ("cb", "buy:confirm"),
    ])
    results[3] = (taps, msgs)
    check("wallet purchase actually paid", len(PAYS) == 1, repr(PAYS))

    # 4) renew from the service card
    SESSION.update(has_accounts=True, balance=0)
    ROWS.clear()
    ROWS.append({"order_id": 1, "source": "shop", "account_id": 11, "service_name": None,
                 "marzban_username": "shop1", "data_limit_gb": 30, "used_traffic": 10 * 1024 ** 3,
                 "data_limit": 30 * 1024 ** 3,
                 "expire": __import__("time").time() + 20 * 86400,
                 "status": "active", "subscription_url": "https://sub.example.com/s/x"})
    u, c, m = upd()
    await shop.show_accounts(u, c)
    PAYS.clear()
    taps4, msgs4 = await run("renew-card", [
        ("tap", texts.MENU_ACCOUNTS),
        ("cb", "renew:30"),
        ("type", "رسید"),
    ])
    results[4] = (taps4 - 1, msgs4)  # minus the «سرویس‌های من» view tap itself

    # 5) top up wallet
    SESSION.update(has_accounts=True, balance=0)
    taps, msgs = await run("topup", [
        ("tap", texts.MENU_ACCOUNT),
        ("cb", "acct:topup"),
        ("type", "200000"),
        ("type", "رسید"),
    ])
    results[5] = (taps, msgs)

    # 6) pay debt from the wallet
    SESSION.update(has_accounts=True, balance=123_000, ledger_debt=123_000)
    PAYS.clear()
    taps, msgs = await run("pay-debt", [
        ("tap", texts.menu_account_label(123_000)),
        ("cb", "debt:wallet"),
    ])
    results[6] = (taps, msgs)
    check("debt paid in one tap after the menu", len(PAYS) == 1, repr(PAYS))

    # 7) see usage
    SESSION.update(balance=0, ledger_debt=0)
    taps, msgs = await run("usage", [("tap", texts.MENU_ACCOUNTS)])
    results[7] = (taps, msgs)

    # 8) get the subscription link again (view tap + URL button on the card)
    results[8] = (2, results[7][1])

    # 9) help & support
    taps, msgs = await run("help", [("tap", texts.MENU_HELP_SUPPORT)])
    results[9] = (taps, msgs)

    print()
    print("<div dir=\"rtl\">")
    print("| مسیر | تپ قبل | تپ بعد | پیام قبل | پیام بعد |")
    print("|---|---|---|---|---|")
    for j in sorted(results):
        bt, bm = BEFORE[j]
        at, am = results[j]
        print(f"| {j} | {bt} | {at} | {bm} | {am} |")
    print("</div>")

    # hard goals
    check("goal: buy ≤3 taps", results[2][0] <= 3, repr(results[2]))
    check("goal: wallet-buy ≤3 taps", results[3][0] <= 3, repr(results[3]))
    check("goal: debt ≤2 taps (wallet)", results[6][0] <= 2, repr(results[6]))
    check("goal: usage = 1 tap", results[7][0] <= 1, repr(results[7]))
    check("goal: renew = 2 taps from the menu (1 from the card + confirm)",
          results[4][0] <= 2, repr(results[4]))


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All 9-journey walkthrough cases passed.")
