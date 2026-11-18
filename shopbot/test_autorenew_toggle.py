"""Round F (2026-11-18, owner journeys 6+7): the customer can SEE the
standing auto-renew state and TURN IT ON/OFF — until today only the
operator's panel could flip `auto_renew_enabled`, so a family member could
neither activate the standing auto-renew nor stop it.

  * «⋯ بیشتر» shows the state as a button: «⚡ تمدید خودکار: روشن — خاموشش کن»
    / «…خاموش — روشنش کن» (None = old backend → no toggle, as before).
  * Tapping it calls POST /accounts/{id}/auto-renew-toggle and answers with
    the new state; stale/malformed gets the usual «قدیمی شده».
  * The one-shot «⚡ تمدید خودکار (میانگین مصرفم)» stays untouched.

Run (from shopbot/):  ../bot/venv/Scripts/python test_autorenew_toggle.py
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


def make_update(cb_data: str | None = None):
    msg = StubMessage()
    upd = SimpleNamespace(
        effective_message=msg, message=msg,
        effective_user=SimpleNamespace(id=555, username="ali_t", first_name="علی", last_name=None),
    )
    if cb_data is not None:
        async def _answer(*a, **k):
            return None
        upd.callback_query = SimpleNamespace(
            data=cb_data, answer=_answer, edit_message_text=msg.reply_text, message=msg)
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
STATE = {"session": dict(SESSION), "rows": [], "toggles": []}


async def fake_session(update) -> dict:
    return dict(STATE["session"])


class StubBackend:
    async def get(self, path, params=None):
        if path == "/api/shop/bot/accounts":
            return [dict(r) for r in STATE["rows"]]
        raise AssertionError(path)

    async def post(self, path, json=None, timeout: float = 20):
        if path.startswith("/api/shop/bot/accounts/") and path.endswith("/auto-renew-toggle"):
            STATE["toggles"].append((path, dict(json or {})))
            return {"ok": True, "auto_renew_enabled": (json or {}).get("enabled")}
        raise AssertionError(path)


shop.backend = StubBackend()
shop._session = fake_session  # type: ignore[assignment]


def row(**over):
    r = {"order_id": 11, "source": "shop", "account_id": 11,
         "service_name": "گوشی علی", "marzban_username": "shop11",
         "data_limit_gb": 30, "used_traffic": 5 * 1024 ** 3, "data_limit": 30 * 1024 ** 3,
         "expire": None, "status": "active",
         "subscription_url": "https://sub.example.com/s/11",
         "renews_here": True, "can_renew": True, "auto_renew_ok": True}
    r.update(over)
    return r


def buttons_of(msg):
    kb = msg.replies[-1][1]
    if kb is None or not hasattr(kb, "inline_keyboard"):
        return []
    return [(b.text, b.callback_data or "") for row_ in kb.inline_keyboard for b in row_]


async def main() -> None:
    # ── 1. state ON → the panel shows «خاموشش کن» ─────────────────────────
    STATE["rows"] = [row(auto_renew_enabled=True)]
    (upd, ctx), msg = make_update(cb_data="svcmore:11")
    await shop.on_service_more(upd, ctx)
    btns = buttons_of(msg)
    check("ON state → the toggle-off button with the state in words",
          any(t == texts.AUTORENEW_STATE_ON and c == "autotoggle:11:off" for t, c in btns),
          repr(btns))
    check("the one-shot average button is still there",
          any(t == texts.AUTO_RENEW_BUTTON for t, _ in btns), repr(btns))

    # ── 2. tapping it turns the standing auto-renew OFF ───────────────────
    STATE["toggles"].clear()
    (upd, ctx), msg = make_update(cb_data="autotoggle:11:off")
    await shop.on_auto_toggle(upd, ctx)
    check("the toggle endpoint was called with enabled=False",
          STATE["toggles"] and STATE["toggles"][-1][1].get("enabled") is False,
          repr(STATE["toggles"]))
    check("the answer says it is OFF now",
          any("خاموش شد" in r[0] for r in msg.replies), repr([r[0] for r in msg.replies]))

    # ── 3. state OFF → «روشنش کن» → ON ─────────────────────────────────────
    STATE["rows"] = [row(auto_renew_enabled=False)]
    (upd, ctx), msg = make_update(cb_data="svcmore:11")
    await shop.on_service_more(upd, ctx)
    btns = buttons_of(msg)
    check("OFF state → the toggle-on button",
          any(t == texts.AUTORENEW_STATE_OFF and c == "autotoggle:11:on" for t, c in btns),
          repr(btns))
    STATE["toggles"].clear()
    (upd, ctx), msg = make_update(cb_data="autotoggle:11:on")
    await shop.on_auto_toggle(upd, ctx)
    check("tapping ON reaches the endpoint with enabled=True",
          STATE["toggles"] and STATE["toggles"][-1][1].get("enabled") is True,
          repr(STATE["toggles"]))
    check("the answer says it is ON now (and what it will do)",
          any("روشن شد" in r[0] and "سفارش" in r[0] for r in msg.replies),
          repr([r[0] for r in msg.replies]))

    # ── 4. old backend (no state) → NO toggle, one-shot stays ─────────────
    STATE["rows"] = [row(auto_renew_enabled=None)]
    (upd, ctx), msg = make_update(cb_data="svcmore:11")
    await shop.on_service_more(upd, ctx)
    btns = buttons_of(msg)
    check("old backend: no state toggle",
          not any(c.startswith("autotoggle:") for _, c in btns), repr(btns))
    check("old backend: the one-shot button remains",
          any(t == texts.AUTO_RENEW_BUTTON for t, _ in btns), repr(btns))

    # ── 5. malformed callback → the usual stale answer ─────────────────────
    (upd, ctx), msg = make_update(cb_data="autotoggle:x:on")
    await shop.on_auto_toggle(upd, ctx)
    check("malformed autotoggle → «قدیمی شده»",
          any("قدیمی" in r[0] for r in msg.replies), repr(msg.replies))


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All auto-renew toggle cases passed.")
