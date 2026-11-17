"""Regression: an INVALID typed username must be answered, never crash.

Found in review 2026-11-17 (live): the invalid-name branch referenced
`texts.USERNAME_INVALID`, which did not exist — so a customer tapping
«✏️ نام کاربری دلخواه» (or the PAYG start) and typing a Persian name — the
most likely input there is — got an AttributeError into on_error's generic
"technical problem" instead of the rule restated. The ask's own example
(«مثلاً fateme.») carried a dot the pattern rejects, so even copying the
example produced the crash.

  * texts defines USERNAME_INVALID (the crash itself).
  * The preferred-name ask: invalid → the invalid-name reply, state kept
    (retry / «بدون نام» / «انصراف» all still one tap away); valid → back on
    the volume screen.
  * The PAYG username ask: invalid → the same reply, no exception; valid →
    the PAYG start goes through.
  * The example printed in ASK_PREFERRED_NAME satisfies the rule it states.

Run (from shopbot/):
  ../bot/venv/Scripts/python test_invalid_username.py
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


def make_update(text: str | None = None, user_id: int = 777, cb_data: str | None = None):
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
    "payg_enabled": True, "payg_rate_per_gb": 5_000,
    "has_accounts": True, "has_payg_service": False,
}


async def fake_session(update) -> dict:
    return dict(SESSION)


class StubBackend:
    async def get(self, path, params=None):
        raise AssertionError(path)

    async def post(self, path, json=None, timeout: float = 20):
        if path == "/api/shop/bot/payg/start":
            return {}
        raise AssertionError(path)


shop.backend = StubBackend()
shop._session = fake_session  # type: ignore[assignment]


async def main() -> None:
    # ── 1. the text module defines the reply the handlers send ───────────
    check("texts.USERNAME_INVALID exists and is a single string",
          isinstance(texts.USERNAME_INVALID, str), "AttributeError on base")

    # ── 2. preferred-name ask: invalid → answered, state kept ────────────
    (upd, context), msg = make_update(cb_data="uname:ask")
    await shop.on_username_ask(upd, context)
    check("uname:ask opens the ask",
          any("نام کاربری" in r[0] for r in msg.replies), repr(msg.replies))
    msg.text = "فاطمه"  # Persian — fails the [a-zA-Z0-9_-] pattern
    await shop.handle_text(upd, context)
    last = msg.replies[-1][0]
    check("invalid name → the invalid-name reply (no crash)",
          last == texts.USERNAME_INVALID, repr(last))
    check("invalid name keeps the ask state (retry is possible)",
          context.user_data.get(shop._STATE) == shop._STATE_AWAITING_PREFERRED_NAME)
    msg.text = "fateme"
    await shop.handle_text(upd, context)
    check("valid retry re-renders the volume screen",
          context.user_data.get(shop._STATE) == shop._STATE_CHOOSING_VOLUME
          and any("گیگ" in r[0] for r in msg.replies[2:]), repr([r[0] for r in msg.replies]))

    # ── 3. PAYG username ask: invalid → answered; valid → start runs ─────
    (upd, context), msg = make_update("فاطمه")
    context.user_data[shop._STATE] = shop._STATE_AWAITING_PAYG_USERNAME
    context.user_data[shop._PENDING_PAYG_PAYLOAD] = {"telegram_username": "ali_t"}
    await shop.handle_text(upd, context)
    last = msg.replies[-1][0]
    check("PAYG: invalid name → the invalid-name reply (no crash)",
          last == texts.USERNAME_INVALID, repr(last))
    check("PAYG: invalid name keeps the ask state",
          context.user_data.get(shop._STATE) == shop._STATE_AWAITING_PAYG_USERNAME)
    msg.text = "fateme"
    await shop.handle_text(upd, context)
    check("PAYG: valid name → start confirmed",
          any("فعال شد" in r[0] for r in msg.replies[1:]), repr([r[0] for r in msg.replies]))

    # ── 4. the ask's own example satisfies the rule it states ────────────
    m = re.search(r"مثلاً\s+([A-Za-z0-9_.\-]+)", texts.ASK_PREFERRED_NAME)
    check("ASK_PREFERRED_NAME prints an example", m is not None,
          repr(texts.ASK_PREFERRED_NAME))
    if m:
        check("the printed example passes the username pattern itself",
              re.fullmatch(r"[a-zA-Z0-9_-]{3,30}", m.group(1)) is not None,
              repr(m.group(1)))


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All invalid-username cases passed.")
