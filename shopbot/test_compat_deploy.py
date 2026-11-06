"""R5 deploy-compatibility — NEW bot against an OLD backend's response shapes.

The deploy ships backend and bot as separate services, so there IS a window
(new shopbot image, old backend image) — this pins what that window looks
like: no crash anywhere, only documented degradation (docs/deploys/
2026-11-06_shopbot_ux.md):

  * session without has_accounts / has_payg_service (fields the old backend
    never sent) → menu renders with the plain «خرید» label; the buy screen
    shows the optional-username button instead of «سرویس جداگانه».
  * account rows without renews_here → the renew button renders on every
    card (pre-0aa626d behaviour — renewal still extends renewable_account).
  * order intent of the old shape (no new fields) → the whole buy flow and
    the wallet confirm work unchanged.

Run (from shopbot/):
  ../bot/venv/Scripts/python test_compat_deploy.py
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


# The OLD backend's ShopBotSession: everything EXCEPT the three round-1
# fields (has_accounts / has_payg_service) — ledger_debt/debt_* existed.
SESSION_OLD = {
    "balance": 40_000, "trial_available": False, "shop_name": None, "support_handle": "vpn_support",
    "is_blocked": False, "is_open": True,
    "min_topup": 50_000, "max_topup": 5_000_000, "card_number": "6037-9911-2222-3333",
    "card_holder": None, "approval_eta_minutes": 30, "phone": None,
    "price_per_gb": 10_000, "min_gb": 5, "max_gb": 100, "plan_duration_days": 30,
    "ledger_debt": 0, "debt_breakdown": [], "debt_exact": True,
    "payg_enabled": False, "payg_rate_per_gb": 0,
}

# OLD account row: no renews_here, no status (status arrived with F11).
ACCOUNT_OLD = {
    "account_id": 101, "service_name": None, "marzban_username": "shop10",
    "source": "shop", "used_traffic": int(5 * 1024**3), "data_limit": int(30 * 1024**3),
    "expire": None, "subscription_url": "https://sub.example.com/s/abc123",
}

# OLD order intent: same fields the old backend returned (payable_from_wallet
# etc. all predate round 1).
ORDER_OLD = {
    "order_id": 77, "creates_new": False, "data_limit_gb": 10,
    "duration_days": 30, "price": 100_000, "balance": 0,
    "shortfall": 100_000, "payable_from_wallet": False,
    "card_number": "6037-9911-2222-3333", "card_holder": None,
    "approval_eta_minutes": 30,
}


async def fake_session(update) -> dict:
    return dict(SESSION_OLD)


class StubBackend:
    async def get(self, path, params=None):
        if path == "/api/shop/bot/wallet":
            return []
        if path == "/api/shop/bot/accounts":
            return [dict(ACCOUNT_OLD)]
        raise AssertionError(path)

    async def post(self, path, json=None, timeout: float = 20):
        if path == "/api/shop/bot/orders":
            return dict(ORDER_OLD)
        raise AssertionError(path)


shop.backend = StubBackend()
shop._session = fake_session  # type: ignore[assignment]


async def main() -> None:
    # /start on an old backend: menu renders; the money door still shows.
    (upd, context), msg = make_update()
    upd.callback_query = None
    await shop.start(upd, context)
    kb = msg.replies[-1][1]
    labels = [b.text for row in kb.keyboard for b in row]
    check("old-backend session: menu renders", len(msg.replies) >= 1, repr(msg.replies))
    check("old-backend session: money door label present",
          texts.MENU_ACCOUNT in labels, repr(labels))
    check("old-backend session: buy label is plain «خرید سرویس» (no «/ تمدید»)",
          texts.MENU_BUY in labels and texts.MENU_BUY_RENEW not in labels, repr(labels))

    # Buy screen: the conditional buttons degrade to the username pre-pick.
    (upd, context), msg = make_update(texts.MENU_BUY)
    await shop.handle_text(upd, context)
    inline = msg.replies[-1][1].inline_keyboard
    cbs = [b.callback_data for row in inline for b in row]
    check("old backend: buy screen renders with the username pre-pick (not «سرویس جداگانه»)",
          "uname:ask" in cbs and "menu:newservice" not in cbs, repr(cbs))

    # Services list: renews_here missing → renew button on the card (old
    # behaviour; renewal still extends renewable_account server-side).
    (upd, context), msg = make_update(texts.MENU_ACCOUNTS)
    await shop.handle_text(upd, context)
    inline = msg.replies[-1][1].inline_keyboard
    cbs = [b.callback_data for row in inline for b in row if b.callback_data]
    check("old backend: card renders; renew button present (renews_here defaults True)",
          any(c.startswith("renew:") for c in cbs) and any(c.startswith("svcmore:") for c in cbs),
          repr(cbs))

    # «حساب من» debt/wallet views render on the old session shape.
    (upd, context), msg = make_update(texts.MENU_ACCOUNT)
    await shop.handle_text(upd, context)
    check("old backend: wallet flavour of «حساب من» renders",
          any("موجودی" in r[0] for r in msg.replies), repr([r[0] for r in msg.replies]))

    # Full buy flow on the old intent: volume → order POST → payment screen.
    (upd, context), msg = make_update(cb_data="vol:10")
    await shop.on_volume_choice(upd, context)
    check("old backend: order accepted, card-to-card payment screen shown",
          any("واریز" in r[0] for r in msg.replies), repr([r[0] for r in msg.replies]))

    # Debt flow (old session carried ledger_debt too).
    SESSION_OLD["ledger_debt"] = 90_000
    (upd, context), msg = make_update(texts.MENU_ACCOUNT)
    await shop.handle_text(upd, context)
    check("old backend: debt flavour renders with the receipt button",
          any("ماندهٔ پرداخت‌نشده" in r[0] for r in msg.replies), repr([r[0] for r in msg.replies]))
    SESSION_OLD["ledger_debt"] = 0


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All compat (new bot / old backend) cases passed.")
