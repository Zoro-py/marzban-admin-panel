"""Per-account renewal in the bot (feat/shopbot-ux-rebuild):

  * With a backend that advertises per_account_renewal, EVERY card that can
    take a volume purchase carries its own renew button  ren:<account_id>:<gb>
    (a customer with 4 services sees 4 working buttons, not one).
  * A card that cannot take it (can_renew False: unlimited / metered / dead)
    has no renew button and no «tap renew» sentence.
  * Tapping it creates the order WITH target_account_id and says which service
    the volume goes to.
  * Against an OLD backend (no capability bit) the bot never sends the field
    (it would be dropped silently and the wrong service renewed) and keeps the
    old one-card behaviour.
  * Malformed / stale payloads and a vanished target are friendly.
  * Legacy renew:<gb> buttons in old chats still work (no target).

Run (from shopbot/):  python test_per_account_renew.py
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
from api_client import ShopApiError  # noqa: E402

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
    if cb_data is not None:
        async def _answer(*a, **k):
            return None

        async def _edit(*a, **k):
            return None
        upd.callback_query = SimpleNamespace(
            data=cb_data, answer=_answer, edit_message_text=_edit,
            edit_message_reply_markup=_edit, message=msg)
    return (upd, SimpleNamespace(args=[], user_data={}, chat_data={})), msg


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
            if STATE["order_error"] is not None:
                raise STATE["order_error"]
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


def reset(**session_over):
    STATE["session"] = {**SESSION, **session_over}
    STATE["rows"] = []
    STATE["orders"] = []
    STATE["order_error"] = None


async def main() -> None:
    # 1. four services -> four working renew buttons, each names ITS account
    reset()
    STATE["rows"] = [row(11, 30), row(12, 50), row(13, 20), row(14, 100)]
    (upd, ctx), msg = make_update()
    await shop.show_accounts(upd, ctx)
    check("four cards", len(msg.replies) == 4, repr(len(msg.replies)))
    expected = ["ren:11:30", "ren:12:50", "ren:13:20", "ren:14:100"]
    got = [next((c for c in cbs(m) if c.startswith("ren:")), None) for _, m in msg.replies]
    check("each card renews ITSELF (ren:<id>:<gb>)", got == expected, repr(got))
    check("no ambiguous legacy renew: button on any card",
          not any(c.startswith("renew:") for _, m in msg.replies for c in cbs(m)))

    # 2. a card that cannot take a volume purchase: no button, no promise
    reset()
    STATE["rows"] = [row(11, 30), row(12, 30, can_renew=False, used_traffic=30 * 1024 ** 3)]
    (upd, ctx), msg = make_update()
    await shop.show_accounts(upd, ctx)
    check("can_renew False -> no renew button", not any(c.startswith(("ren:", "renew:")) for c in cbs(msg.replies[1][1])))
    check("...and no «tap renew» sentence", texts.RENEW_HINT_ENDED not in msg.replies[1][0])
    check("...but link and more remain", any(c.startswith("svcmore:") for c in cbs(msg.replies[1][1])))

    # 3. OLD backend (no capability bit): previous behaviour, one card only
    reset(per_account_renewal=False)
    STATE["rows"] = [row(11, renews_here=True, can_renew=None), row(12, renews_here=False, can_renew=None)]
    (upd, ctx), msg = make_update()
    await shop.show_accounts(upd, ctx)
    check("old backend: legacy renew: on the renews_here card",
          any(c.startswith("renew:") for c in cbs(msg.replies[0][1])))
    check("old backend: none on the other card", not any(c.startswith(("renew:", "ren:")) for c in cbs(msg.replies[1][1])))

    # 4. tap -> order carries the target, screen names the service
    reset()
    (upd, ctx), msg = make_update(cb_data="ren:12:50")
    await shop.on_renew_target(upd, ctx)
    check("order created once", len(STATE["orders"]) == 1, repr(STATE["orders"]))
    o = STATE["orders"][0] if STATE["orders"] else {}
    check("payload: target_account_id == 12", o.get("target_account_id") == 12, repr(o))
    check("payload: volume 50, force_new False", o.get("data_limit_gb") == 50 and o.get("force_new_service") is False, repr(o))
    shown = "\n".join(t for t, _ in msg.replies)
    check("payment screen says WHICH service", "تمدید «گوشی من»" in shown, repr(shown[:200]))

    # 5. against an old backend the field is NEVER sent
    reset(per_account_renewal=False)
    (upd, ctx), msg = make_update(cb_data="ren:12:50")
    await shop.on_renew_target(upd, ctx)
    check("old backend: no target_account_id in the payload",
          len(STATE["orders"]) == 1 and "target_account_id" not in STATE["orders"][0], repr(STATE["orders"]))

    # 6. malformed / stale
    for bad in ("ren:", "ren:x:30", "ren:12", "ren:12:abc"):
        reset()
        (upd, ctx), msg = make_update(cb_data=bad)
        await shop.on_renew_target(upd, ctx)
        check(f"{bad!r} -> «قدیمی شده», no order",
              msg.replies and msg.replies[-1][0] == texts.STALE_BUTTON and not STATE["orders"], repr(msg.replies))

    # 7. the target vanished / is not theirs: friendly, nothing created
    reset()
    STATE["order_error"] = ShopApiError("TARGET_NOT_FOUND: service not found", 404)
    (upd, ctx), msg = make_update(cb_data="ren:12:50")
    await shop.on_renew_target(upd, ctx)
    check("404 -> RENEW_TARGET_GONE (not «مشکل فنی»)",
          msg.replies and msg.replies[-1][0] == texts.RENEW_TARGET_GONE, repr(msg.replies))
    reset()
    STATE["order_error"] = ShopApiError("This service is no longer active.", 409)
    (upd, ctx), msg = make_update(cb_data="ren:12:50")
    await shop.on_renew_target(upd, ctx)
    check("«no longer active» -> RENEW_TARGET_GONE",
          msg.replies and msg.replies[-1][0] == texts.RENEW_TARGET_GONE, repr(msg.replies))

    # 8. a legacy renew:<gb> button in an old chat still works, with no target
    reset()
    (upd, ctx), msg = make_update(cb_data="renew:30")
    await shop.on_renew(upd, ctx)
    check("legacy renew: -> order without target",
          len(STATE["orders"]) == 1 and "target_account_id" not in STATE["orders"][0], repr(STATE["orders"]))

    # 9. a normal menu buy after a targeted renewal does not inherit the target
    reset()
    (upd, ctx), msg = make_update(cb_data="ren:12:50")
    await shop.on_renew_target(upd, ctx)
    (upd2, ctx2), msg2 = make_update()
    ctx2.user_data = ctx.user_data      # same chat state, then a plain buy starts
    await shop.start_buy(upd2, ctx2)
    check("start_buy clears the target", shop._TARGET_ACCOUNT_ID not in ctx2.user_data, repr(ctx2.user_data))


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All per-account renew cases passed.")
