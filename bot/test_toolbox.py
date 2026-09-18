"""Behavioral smoke test for the operator bot's toolbox: /bill (statement
with refresh + payment handoff), /debts (the payment console on demand) and
the bill refresh/close callbacks. Telegram objects and the backend client
are mocked — read-only paths, nothing posted, no network.

Run from bot/:  venv/Scripts/python test_toolbox.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("ADMIN_CHAT_ID", "777")
os.environ.setdefault("API_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import handlers.bill as bill  # noqa: E402
import handlers.common as common  # noqa: E402
import handlers.debt as debt  # noqa: E402

failures: list[str] = []
ADMIN = 777


def check(label: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class FakeBackend:
    async def get(self, url: str, params=None):
        if url.startswith("/api/notifications/debt-nudge"):
            return {"overdue": [
                {"name": "Ali", "customer_id": 5, "amount": 120000.0, "days": 40},
                {"name": "Sara", "customer_id": 7, "amount": 50000.0, "days": 20},
            ]}
        if url == "/api/customers":
            return [
                {"id": 5, "name": "Ali"},
                {"id": 6, "name": "Alireza"},
                {"id": 7, "name": "Sara"},
            ]
        if url == "/api/accounts":
            return [{"id": 9, "marzban_username": "acc1", "customer_id": 5}]
        if url == "/api/customers/5":
            return {"id": 5, "name": "Ali"}
        if url == "/api/customers/6":
            return {"id": 6, "name": "Alireza"}
        if url == "/api/customers/5/accounts":
            return [{"id": 9, "marzban_username": "acc1", "net_owed": 120000.0}]
        if "ledger/balance" in url and (params or {}).get("customer_id") == 5:
            return {"balance": 120000.0, "gb_pending": 30.5, "pending_amount": 152500.0}
        if url.startswith("/api/ledger"):
            return [
                {"date": "2026-09-18 12:00:00", "type": "credit", "amount": 250000.0, "account_id": 9},
                {"date": "2026-08-22 23:30:01", "type": "charge", "amount": 43287.79, "account_id": 9},
            ]
        raise AssertionError(f"unexpected GET {url} params={params}")

    async def post(self, url: str, json=None):
        raise AssertionError("toolbox paths must never post")


fake = FakeBackend()
bill.backend = fake
debt.backend = fake
common.backend = fake  # resolve_customer/resolve_account ride common's own reference


def make_command(args: list[str]):
    replies: list[tuple[str, object]] = []
    msg = SimpleNamespace(reply_text=async_capture(replies))
    upd = SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=ADMIN), callback_query=None)
    return SimpleNamespace(args=args), upd, replies


def async_capture(into: list):
    async def _capture(text, reply_markup=None, **kwargs):
        into.append((text, reply_markup))
    return _capture


def make_update(data: str, msg_id: int = 100):
    q = SimpleNamespace(data=data, message=SimpleNamespace(message_id=msg_id),
                        edits=[], answered=False)
    q.answer = async_capture([])  # unused for callbacks under test
    q.answer = _noop
    q.edit_message_text = _record_edit(q)
    upd = SimpleNamespace(callback_query=q, effective_chat=SimpleNamespace(id=ADMIN), message=None)
    return q, upd


async def _noop():
    return True


def _record_edit(q):
    async def _edit(text, reply_markup=None):
        q.edits.append((text, reply_markup))
    return _edit


async def main() -> None:
    # 1) /bill by exact name
    ctx, upd, replies = make_command(["Ali"])
    await bill.bill_command(upd, ctx)
    text, markup = replies[-1]
    check("bill by name renders the statement", "صورتحساب Ali" in text)
    check("bill shows posted balance and owing status", "120,000 T" in text and "بدهکار" in text)
    check("bill shows not-yet-invoiced usage with Toman", "30.5 GB" in text and "152,500 T" in text)
    check("bill shows the settle-now total", "272,500 T" in text)
    check("bill lists recent transactions with account names", "acc1" in text and "بدهی" in text and "پرداخت" in text)
    cb = callbacks(markup)
    check("bill has refresh + payment handoff + close", "bill:refresh:5" in cb and "debtnudge:5" in cb and "bill:close" in cb)

    # 2) /bill by marzban username
    ctx, upd, replies = make_command(["acc1"])
    await bill.bill_command(upd, ctx)
    text, _ = replies[-1]
    check("bill resolves a marzban username to its customer", "صورتحساب Ali" in text)

    # 3) ambiguous name → disambiguation buttons ("a" matches all three;
    #    "ali" itself is now an exact match for Ali, by design)
    ctx, upd, replies = make_command(["a"])
    await bill.bill_command(upd, ctx)
    text, markup = replies[-1]
    check("ambiguous name offers picker buttons",
          "bill:show:5" in callbacks(markup) and "bill:show:6" in callbacks(markup))

    # 4) /bill with no args → debtor quick-pick
    ctx, upd, replies = make_command([])
    await bill.bill_command(upd, ctx)
    text, markup = replies[-1]
    check("no-arg bill offers current debtors", "bill:show:5" in callbacks(markup) and "bill:show:7" in callbacks(markup))

    # 5) refresh callback re-renders the bill live
    q, upd = make_update("bill:refresh:5")
    await bill.bill_callback(upd, context := SimpleNamespace())
    text, markup = q.edits[-1]
    check("bill refresh re-renders the statement", "صورتحساب Ali" in text and "bill:refresh:5" in callbacks(markup))

    # 6) /debts opens the payment console list
    ctx, upd, replies = make_command([])
    await debt.debts_command(upd, ctx)
    text, markup = replies[-1]
    cb = callbacks(markup)
    check("/debts renders the debtor list with console nav",
          "بدهی‌های قدیمی" in text and "debtnudge:5" in cb and "debthub:refresh" in cb and "debtdo:close" in cb)

    # 7) bill close button closes
    q, upd = make_update("bill:close", msg_id=600)
    await bill.bill_callback(upd, SimpleNamespace())
    text, _ = q.edits[-1]
    check("bill close closes", "بسته شد" in text)


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All toolbox cases passed.")
