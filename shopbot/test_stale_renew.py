"""M-2 (2026-11-17): a RENEW button carries its creation time, and a tap on
an abandoned one refuses instead of silently creating an order.

The documented harm (T0 #3, M-2 in ZCODE_SHOPBOT_REVIEW.md): a warning push
from a week ago still sits in the chat; tapping it creates an order with
that day's volume — after the customer may have already renewed elsewhere.
The fix: the warning sweep (backend) and the service cards (bot) embed the
button's creation timestamp in the callback data (`renew:<gb>:<ts>` /
`ren:<id>:<gb>:<ts>`); the bot refuses taps older than
_RENEW_BUTTON_MAX_AGE_S (7 days) with a calm sentence and clears the dead
markup. Buttons WITHOUT a timestamp (old chats, the old-backend window)
keep working — compat, deliberate.

Run (from shopbot/):  ../bot/venv/Scripts/python test_stale_renew.py
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
    def __init__(self):
        self.replies: list[tuple] = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append((text, reply_markup))


def make_update(cb_data: str | None = None):
    msg = StubMessage()
    upd = SimpleNamespace(
        effective_message=msg, message=msg,
        effective_user=SimpleNamespace(id=555, username="ali_t", first_name="علی", last_name=None),
    )
    cleared: list[bool] = []
    if cb_data is not None:
        async def _answer(*a, **k):
            return None

        async def _edit(*a, **k):
            cleared.append(True)
        upd.callback_query = SimpleNamespace(
            data=cb_data, answer=_answer, edit_message_text=_edit,
            edit_message_reply_markup=_edit, message=msg)
    return (upd, SimpleNamespace(args=[], user_data={}, chat_data={})), msg, cleared


SESSION = {
    "balance": 0, "trial_available": False, "shop_name": None, "support_handle": None,
    "is_blocked": False, "is_open": True,
    "min_topup": 50_000, "max_topup": 5_000_000, "card_number": "6037", "card_holder": "x",
    "approval_eta_minutes": 30, "price_per_gb": 5_000, "min_gb": 5, "max_gb": 100,
    "plan_duration_days": 30, "ledger_debt": 0, "debt_breakdown": [], "debt_exact": True,
    "payg_enabled": False, "payg_rate_per_gb": 0, "has_accounts": True, "has_payg_service": False,
    "per_account_renewal": True,
}
STATE = {"session": dict(SESSION), "rows": [], "orders": [], "order_error": None}


async def fake_session(update) -> dict:
    return dict(STATE["session"])


class StubBackend:
    async def get(self, path, params=None):
        if path == "/api/shop/bot/accounts":
            return [dict(r) for r in STATE["rows"]]
        raise AssertionError(path)

    async def post(self, path, json=None, timeout: float = 20):
        if path == "/api/shop/bot/orders":
            STATE["orders"].append(dict(json))
            return {"order_id": 9, "data_limit_gb": json["data_limit_gb"], "duration_days": 30,
                    "price": int(json["data_limit_gb"] * 5000), "balance": 0,
                    "shortfall": int(json["data_limit_gb"] * 5000), "payable_from_wallet": False,
                    "creates_new": False, "card_number": "6037", "card_holder": "x",
                    "approval_eta_minutes": 30,
                    "target_label": "گوشی من" if json.get("target_account_id") else None}
        raise AssertionError(path)


shop.backend = StubBackend()
shop._session = fake_session  # type: ignore[assignment]


def row(account_id: int, gb: int = 30, **over) -> dict:
    r = {
        "order_id": account_id, "source": "shop", "account_id": account_id,
        "service_name": f"svc{account_id}", "marzban_username": f"shop{account_id}",
        "data_limit_gb": gb, "used_traffic": 5 * 1024 ** 3, "data_limit": gb * 1024 ** 3,
        "expire": int(time.time()) + 20 * 86400, "status": "active",
        "subscription_url": f"https://sub.example.com/s/{account_id}",
        "renews_here": False, "can_renew": True,
    }
    r.update(over)
    return r


def cbs(markup) -> list[str]:
    if markup is None or not hasattr(markup, "inline_keyboard"):
        return []
    return [b.callback_data or "" for r_ in markup.inline_keyboard for b in r_]


NOW = int(time.time())
DAY = 86400


async def main() -> None:
    # ── 1. the refusal text exists ────────────────────────────────────────
    check("texts.RENEW_BUTTON_STALE exists", isinstance(getattr(texts, "RENEW_BUTTON_STALE", None), str),
          "AttributeError on base")

    # ── 2. ren:<id>:<gb>:<ts> FRESH → the order goes through ──────────────
    STATE["orders"].clear()
    (upd, ctx), msg, _ = make_update(f"ren:11:30:{NOW}")
    await shop.on_renew_target(upd, ctx)
    check("fresh ren: with ts creates the order",
          len(STATE["orders"]) == 1 and STATE["orders"][0].get("target_account_id") == 11,
          repr(STATE["orders"]))
    check("fresh ren: with ts lands on the payment screen",
          any("واریز کنید" in r[0] for r in msg.replies), repr([r[0] for r in msg.replies]))

    # ── 3. ren: with an 8-day-old ts → refused, markup cleared, no order ──
    STATE["orders"].clear()
    (upd, ctx), msg, cleared = make_update(f"ren:11:30:{NOW - 8 * DAY}")
    await shop.on_renew_target(upd, ctx)
    check("stale ren: → the calm stale-refusal sentence",
          any("قدیمی است" in r[0] for r in msg.replies), repr([r[0] for r in msg.replies]))
    check("stale ren: clears the dead markup", bool(cleared), repr(cleared))
    check("stale ren: creates NO order", len(STATE["orders"]) == 0, repr(STATE["orders"]))

    # ── 4. renew:<gb>:<ts> FRESH → goes through ───────────────────────────
    STATE["orders"].clear()
    (upd, ctx), msg, _ = make_update(f"renew:30:{NOW}")
    await shop.on_renew(upd, ctx)
    check("fresh renew: with ts creates the order (no target)",
          len(STATE["orders"]) == 1 and "target_account_id" not in STATE["orders"][0],
          repr(STATE["orders"]))
    check("fresh renew: with ts lands on the payment screen",
          any("واریز کنید" in r[0] for r in msg.replies), repr([r[0] for r in msg.replies]))

    # ── 5. renew: with an 8-day-old ts → refused, never silence ───────────
    STATE["orders"].clear()
    (upd, ctx), msg, cleared = make_update(f"renew:30:{NOW - 8 * DAY}")
    await shop.on_renew(upd, ctx)
    check("stale renew: → the calm refusal (was: silence)",
          any("قدیمی است" in r[0] for r in msg.replies), repr([r[0] for r in msg.replies]))
    check("stale renew: clears the dead markup", bool(cleared), repr(cleared))
    check("stale renew: creates NO order", len(STATE["orders"]) == 0, repr(STATE["orders"]))

    # ── 6. the 7-day boundary: 6 days is still fresh ──────────────────────
    STATE["orders"].clear()
    (upd, ctx), msg, _ = make_update(f"ren:11:30:{NOW - 6 * DAY}")
    await shop.on_renew_target(upd, ctx)
    check("6-day-old ren: still works (inside the window)",
          len(STATE["orders"]) == 1, repr(STATE["orders"]))

    # ── 7. compat: buttons without a timestamp keep working ───────────────
    STATE["orders"].clear()
    (upd, ctx), msg, _ = make_update("ren:11:30")
    await shop.on_renew_target(upd, ctx)
    check("no-ts ren: (old chats / old backend) still creates the order",
          len(STATE["orders"]) == 1, repr(STATE["orders"]))
    STATE["orders"].clear()
    (upd, ctx), msg, _ = make_update("renew:30")
    await shop.on_renew(upd, ctx)
    check("no-ts renew: still creates the order", len(STATE["orders"]) == 1, repr(STATE["orders"]))

    # ── 8. malformed stays the old calm answer ────────────────────────────
    (upd, ctx), msg, _ = make_update("ren:x:30")
    await shop.on_renew_target(upd, ctx)
    check("malformed ren: → «قدیمی شده»",
          any("قدیمی شده" in r[0] for r in msg.replies), repr([r[0] for r in msg.replies]))

    # ── 9. the card RENDERER stamps fresh ts on its renew buttons ─────────
    STATE["rows"] = [row(11, 30)]
    (upd, ctx), msg, _ = make_update()
    await shop.show_accounts(upd, ctx)
    got = next((c for c in cbs(msg.replies[-1][1]) if c.startswith("ren:")), None)
    parts = got.split(":") if got else []
    check("card ren: button carries a fresh ts (ren:<id>:<gb>:<ts>)",
          len(parts) == 4 and parts[1] == "11" and parts[2] == "30"
          and abs(int(parts[3]) - NOW) < 120, repr(got))
    STATE["session"] = {**SESSION, "per_account_renewal": False}
    STATE["rows"] = [row(11, 30, renews_here=True, can_renew=None)]
    (upd, ctx), msg, _ = make_update()
    await shop.show_accounts(upd, ctx)
    got = next((c for c in cbs(msg.replies[-1][1]) if c.startswith("renew:")), None)
    parts = got.split(":") if got else []
    check("old-backend card renew: button carries a fresh ts too",
          len(parts) == 3 and parts[1] == "30" and abs(int(parts[2]) - NOW) < 120, repr(got))
    STATE["session"] = dict(SESSION)


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All stale-renew cases passed.")
