"""Behavioral smoke test for the Telegram debt console (handlers/debt.py):
mocks Telegram objects AND the backend API client — the payment is
CAPTURED, never posted, no network touched.

Run from bot/:  venv/Scripts/python test_debt_console.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

# Env must exist BEFORE importing handlers/api_client (they read these at
# import time; the test never opens a real connection).
os.environ.setdefault("ADMIN_CHAT_ID", "777")
os.environ.setdefault("API_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ.setdefault("BOT_TOKEN", "")
os.environ.setdefault("SHOP_BOT_TOKEN", "")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import handlers.debt as debt  # noqa: E402

failures: list[str] = []
ADMIN = 777


def check(label: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class FakeQuery:
    def __init__(self, data: str, msg_id: int = 100):
        self.data = data
        self.message = SimpleNamespace(message_id=msg_id)
        self.edits: list[tuple[str, object]] = []
        self.answered = False

    async def answer(self) -> None:
        self.answered = True

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))


def make_update(data: str, msg_id: int = 100):
    q = FakeQuery(data, msg_id)
    upd = SimpleNamespace(callback_query=q, effective_chat=SimpleNamespace(id=ADMIN), message=None)
    return q, upd


class FakeBackend:
    """Routes the exact URLs the console reads; posts are captured."""
    remaining_customer = 120000.0

    def __init__(self) -> None:
        self.posts: list[tuple] = []
        self.fail_next_post = False

    async def get(self, url: str):
        if url.startswith("/api/notifications/debt-nudge"):
            return {"overdue": [
                {"name": "Ali", "customer_id": 5, "amount": FakeBackend.remaining_customer, "days": 40},
                {"name": "Sara", "customer_id": 6, "amount": 50000.0, "days": 20},
            ], "accruing": [
                {"name": "khanevadeh", "customer_id": 7, "kind": "family", "posted": 150000.0, "pending": 900000.0, "amount": 1050000.0},
                {"name": "Solo", "customer_id": 8, "kind": "individual", "posted": 0.0, "pending": 50000.0, "amount": 50000.0},
            ]}
        if url == "/api/customers/5":
            return {"name": "Ali", "balance": FakeBackend.remaining_customer}
        if url == "/api/customers/6":
            return {"name": "Sara", "balance": 50000.0}
        if url == "/api/customers/5/accounts":
            return [{"id": 9, "marzban_username": "acc1", "net_owed": FakeBackend.remaining_customer}]
        if url == "/api/groups":
            return []
        if url == "/api/accounts/9":
            return {"marzban_username": "acc1"}
        if "account_id=9" in url:
            return {"balance": 120000.0}
        if "customer_id=5" in url:
            return {"balance": FakeBackend.remaining_customer}
        if "customer_id=6" in url:
            return {"balance": 50000.0}
        raise AssertionError(f"unexpected GET {url}")

    async def post(self, url: str, json=None):
        if self.fail_next_post:
            self.fail_next_post = False
            raise RuntimeError("backend unreachable")
        self.posts.append((url, json))
        FakeBackend.remaining_customer = 0.0  # the payment posts
        return {}


fake = FakeBackend()
debt.backend = fake

deleted: list[int] = []


async def _delete_message(chat_id, message_id):
    deleted.append(message_id)
    return True


edits_via_bot: list[tuple[str, object]] = []


async def _edit_message_text(chat_id, message_id, text, reply_markup=None):
    edits_via_bot.append((text, reply_markup))


context = SimpleNamespace(bot=SimpleNamespace(delete_message=_delete_message, edit_message_text=_edit_message_text))


async def main() -> None:
    # 0) the hub shows the quiet «accruing» section — text only, no extra buttons
    text0, markup0 = await debt._list_content()
    check("hub lists overdue debtors as buttons", "debtnudge:5" in callbacks(markup0) and "debtnudge:6" in callbacks(markup0))
    check("accruing customers are NOT buttons (nothing aged to pay against)",
          "debtnudge:7" not in callbacks(markup0) and "debtnudge:8" not in callbacks(markup0))
    check("accruing section header with count + total", "در جریان" in text0 and "2 نفر" in text0 and "1,100,000" in text0)
    check("family is named and tagged, biggest first",
          "khanevadeh 👨‍👩‍👧 — 1,050,000" in text0 and text0.index("khanevadeh") < text0.index("Solo"))
    check("posted/pending parts are spelled out", "ثبت‌شده 150,000" in text0 and "در جریان 900,000" in text0)

    class _NoOverdue(FakeBackend):
        async def get(self, url):
            data = await super().get(url)
            if url.startswith("/api/notifications/debt-nudge"):
                data = dict(data, overdue=[])
            return data
    saved, debt.backend = debt.backend, _NoOverdue()
    text_e, markup_e = await debt._list_content()
    debt.backend = saved
    check("no overdue: still says so AND still shows the accruing section",
          "کاری نیست" in text_e and "khanevadeh" in text_e)

    # 1) tapping a debtor on the nudge message → bucket breakdown
    q, upd = make_update("debtnudge:5")
    await debt.debt_nudge_callback(upd, context)
    text, markup = q.edits[-1]
    check("debtor screen shows the total", "بدهی کل" in text)
    check("bucket button routes to the account", "debtpay:a:9:5" in callbacks(markup))
    check("debtor screen has back-to-list and close", "debthub:refresh" in callbacks(markup) and "debtdo:close" in callbacks(markup))

    # 2) tapping the bucket → amount screen
    q, upd = make_update("debtpay:a:9:5")
    await debt.debt_pay_callback(upd, context)
    text, markup = q.edits[-1]
    check("amount screen asks for the figure", "مبلغ را به تومان" in text)
    cb = callbacks(markup)
    check("full-amount button routes through the confirm screen", "debtdo:confirm:120000:a:9:5" in cb)
    check("amount screen has back and close", "debtnudge:5" in cb and "debtdo:close" in cb)

    # 3) confirm screen — nothing posted yet
    q, upd = make_update("debtdo:confirm:120000:a:9:5")
    await debt.debt_do_callback(upd, context)
    text, markup = q.edits[-1]
    check("confirm screen is the final yes/no step", "تأیید نهایی" in text and fake.posts == [])
    check("post button carries the exact confirmed amount", "debtdo:post:120000:a:9:5" in callbacks(markup))

    # 4) ✅ ثبت → credit captured with the web's attribution, console returns
    #    to the refreshed list with a success flash
    q, upd = make_update("debtdo:post:120000:a:9:5")
    await debt.debt_do_callback(upd, context)
    check("credit posted through /api/ledger with web attribution",
          fake.posts == [("/api/ledger", {"type": "credit", "amount": 120000.0, "customer_id": 5,
                                          "account_id": 9, "note": "Payment recorded via bot — اکانت acc1"})])
    text, markup = q.edits[-1]
    check("lands back on the live list with the success flash",
          "✅ پرداخت" in text and "بدهی‌های قدیمی" in text and "مانده مشتری" in text)

    # 5) settled guard: tapping the now-paid debtor explains and lands on the list
    q, upd = make_update("debtnudge:5", msg_id=200)
    await debt.debt_nudge_callback(upd, context)
    text, markup = q.edits[-1]
    check("paid debtor gets the settled guard + the list", "تسویه شده" in text and "بدهی‌های قدیمی" in text)

    # 6) over-payment warning on the confirm screen
    q, upd = make_update("debtdo:confirm:150000:a:9:5", msg_id=300)
    await debt.debt_do_callback(upd, context)
    text, markup = q.edits[-1]
    check("over-payment warns the surplus becomes credit", "اعتبار" in text)
    check("post button carries the larger amount", "debtdo:post:150000:a:9:5" in callbacks(markup))

    # 7) typed custom amount: user text is deleted, the CONSOLE edits into confirm
    q, upd = make_update("debtpay:a:9:5", msg_id=400)
    await debt.debt_pay_callback(upd, context)
    typed = SimpleNamespace(text="150,000", message_id=501)
    upd_typed = SimpleNamespace(effective_chat=SimpleNamespace(id=ADMIN), message=typed)
    await debt.debt_amount_handler(upd_typed, context)
    check("typed message deleted to keep the chat clean", deleted == [501])
    text, markup = edits_via_bot[-1]
    check("typed amount lands on the confirm screen via the console edit", "تأیید نهایی" in text)
    check("confirm carries the typed amount", "debtdo:post:150000:a:9:5" in callbacks(markup))

    # 8) close button really closes and clears the bridge
    q, upd = make_update("debtdo:close", msg_id=400)
    await debt.debt_do_callback(upd, context)
    text, _ = q.edits[-1]
    check("close closes the console", "بسته شد" in text)
    check("close clears the typed-amount bridge", 777 not in debt._awaiting)

    # 9) stale old-format button says so instead of acting
    q, upd = make_update("debtdo:full", msg_id=500)
    await debt.debt_do_callback(upd, context)
    text, _ = q.edits[-1]
    check("stale old-format button is refused politely", "نسخه‌ی قبلی" in text)

    # 10) BOT-DEBT-1: a double-tap on the same ✅ ثبت button must NOT post the
    #     credit twice. PTB processes updates sequentially, so both taps reach
    #     the handler one after the other with the same callback_data — the
    #     ledger is append-only, so the second post would silently invent
    #     money. The second tap must be refused and the console must land
    #     back on the live list, not on a dead end.
    FakeBackend.remaining_customer = 120000.0
    fake.posts.clear()
    q, upd = make_update("debtdo:post:120000:a:9:5", msg_id=600)
    await debt.debt_do_callback(upd, context)
    check("first tap posts once", len(fake.posts) == 1)
    q2, upd2 = make_update("debtdo:post:120000:a:9:5", msg_id=600)
    await debt.debt_do_callback(upd2, context)
    check("double-tap does NOT post a second credit", len(fake.posts) == 1)
    text, _ = q2.edits[-1]
    check("double-tap says so and lands on the live list, not a dead end",
          "تکراری" in text or "ثبت شد" in text)

    # 11) a FAILED post unblocks the retry: the guard must not wedge the flow
    #     after a transient backend failure.
    FakeBackend.remaining_customer = 120000.0
    fake.posts.clear()
    fake.fail_next_post = True
    q, upd = make_update("debtdo:post:120000:a:9:5", msg_id=700)
    await debt.debt_do_callback(upd, context)
    check("failed post shows the failure", any("شکست" in t for t, _ in q.edits))
    fake.fail_next_post = False
    await debt.debt_do_callback(upd, context)  # immediate retry, same button
    check("retry after a failed post goes through", len(fake.posts) == 1)


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All debt-console cases passed.")
