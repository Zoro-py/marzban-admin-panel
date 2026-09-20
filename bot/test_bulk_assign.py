"""Behavioral test for the /bulk assignment step: the confirm keyboard must
offer the new-family-customer default, cust=/group= must resolve before
anything is created, and the asnew route must reuse-or-create the customer
and attach it to the batch. Telegram objects and the backend client are
mocked — nothing is created anywhere real.

Run from bot/:  venv/Scripts/python test_bulk_assign.py
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

import handlers.bulk as bulk  # noqa: E402

failures: list[str] = []
ADMIN = 777


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class FakeBackend:
    def __init__(self):
        self.customers = [{"id": 5, "name": "Ali"}, {"id": 6, "name": "boojar"}]
        self.groups = [{"id": 2, "name": "company-x"}]
        self.posts: list[tuple[str, dict]] = []

    async def get(self, url: str, params=None):
        if url == "/api/customers":
            return self.customers
        if url == "/api/groups":
            return self.groups
        raise AssertionError(f"unexpected GET {url}")

    async def post(self, url: str, json=None, timeout: float = 20):
        self.posts.append((url, json or {}))
        if url == "/api/accounts/bulk/preview":
            return {"names": [{"marzban_username": "khanevade1", "already_exists": False},
                              {"marzban_username": "khanevade2", "already_exists": False}]}
        if url == "/api/customers":
            new = {"id": 9, "name": json["name"]}
            self.customers.append(new)
            return new
        if url == "/api/accounts/bulk":
            return {"created": 2, "skipped": 0, "failed": 0, "items": [], "notifications_queued": True}
        raise AssertionError(f"unexpected POST {url}")


class FakeMessage:
    def __init__(self):
        self.texts: list[str] = []
        self.markups: list = []

    async def reply_text(self, text, reply_markup=None, **_):
        self.texts.append(text)
        self.markups.append(reply_markup)


class FakeQuery:
    def __init__(self, data: str):
        self.data = data
        self.edits: list[str] = []

    async def answer(self):
        pass

    async def edit_message_text(self, text, **_):
        self.edits.append(text)


def make_update(args: list[str]) -> SimpleNamespace:
    return SimpleNamespace(message=FakeMessage(), effective_user=SimpleNamespace(id=ADMIN),
                           effective_chat=SimpleNamespace(id=ADMIN))


async def scenario_command(fx: FakeBackend, args: list[str], user_data: dict):
    bulk.backend = fx
    update = make_update(args)
    ctx = SimpleNamespace(args=args, user_data=user_data)
    await bulk.bulk_command(update, ctx)
    return update


async def main() -> None:
    print("== /bulk without assignment: safe default keyboard ==")
    fx = FakeBackend()
    user_data: dict = {}
    upd = await scenario_command(fx, ["khanevade", "2"], user_data)
    kb = upd.message.markups[0]
    cbs = callbacks(kb)
    check("offers new-family-customer as the FIRST (safe default) button",
          cbs and cbs[0].startswith("bulk:asnew:"), str(cbs))
    check("offers explicit no-owner create", any(c.startswith("bulk:go:") for c in cbs))
    check("offers cancel", any(c.startswith("bulk:no:") for c in cbs))
    check("preview body carried NO customer/group",
          all("customer_id" not in body and "group_id" not in body for url, body in fx.posts), "")

    print("== /bulk cust=<name>: resolved before creation, direct confirm ==")
    fx2 = FakeBackend()
    user_data2: dict = {}
    upd2 = await scenario_command(fx2, ["khanevade", "2", "cust=Ali"], user_data2)
    kb2 = upd2.message.markups[0]
    cbs2 = callbacks(kb2)
    check("assigned flow shows only Create/Cancel", cbs2[0].startswith("bulk:go:")
          and any(c.startswith("bulk:no:") for c in cbs2), str(cbs2))
    check("assignment label shown", any("customer Ali" in t for t in upd2.message.texts))
    check("no batch created yet", all(u != "/api/accounts/bulk" for u, _ in fx2.posts))

    print("== /bulk cust=<unknown>: refused BEFORE creating anything ==")
    fx3 = FakeBackend()
    upd3 = await scenario_command(fx3, ["khanevade", "2", "cust=Nobody"], {})
    check("no customer created", all(u != "/api/customers" for u, _ in fx3.posts))
    check("no batch posted", all(u != "/api/accounts/bulk" for u, _ in fx3.posts))
    check("operator told what to do", any("No customer 'Nobody'" in t for t in upd3.message.texts))

    print("== /bulk cust=12 + group=2: refused (both given) ==")
    fx4 = FakeBackend()
    upd4 = await scenario_command(fx4, ["khanevade", "2", "cust=12", "group=2"], {})
    check("nothing posted", not fx4.posts)

    print("== asnew callback: creates-or-reuses customer, attaches batch ==")
    fx5 = FakeBackend()
    user_data5: dict = {}
    upd5 = await scenario_command(fx5, ["khanevade", "2"], user_data5)
    token = callbacks(upd5.message.markups[0])[0].split(":")[2]
    q = FakeQuery(f"bulk:asnew:{token}")
    ctx5 = SimpleNamespace(user_data=user_data5)
    await bulk.bulk_callback(SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=ADMIN),
                                             effective_chat=SimpleNamespace(id=ADMIN)), ctx5)
    created = [b for u, b in fx5.posts if u == "/api/accounts/bulk"]
    check("customer created once", sum(1 for u, _ in fx5.posts if u == "/api/customers") == 1)
    check("batch posted WITH the new customer_id",
          len(created) == 1 and created[0].get("customer_id") == 9, str(created))
    check("batch body has no _assign leftovers", created and "_assign_customer" not in created[0])

    print("== asnew with existing same-name customer: reused, not duplicated ==")
    fx6 = FakeBackend()
    fx6.customers.append({"id": 6, "name": "khanevade"})  # already exists by name
    user_data6: dict = {}
    upd6 = await scenario_command(fx6, ["khanevade", "2"], user_data6)
    token6 = callbacks(upd6.message.markups[0])[0].split(":")[2]
    q6 = FakeQuery(f"bulk:asnew:{token6}")
    await bulk.bulk_callback(SimpleNamespace(callback_query=q6, effective_user=SimpleNamespace(id=ADMIN),
                                             effective_chat=SimpleNamespace(id=ADMIN)),
                             SimpleNamespace(user_data=user_data6))
    created6 = [b for u, b in fx6.posts if u == "/api/accounts/bulk"]
    check("no NEW customer created (reused id 6)",
          all(u != "/api/customers" for u, _ in fx6.posts))
    check("batch attached to existing customer 6",
          len(created6) == 1 and created6[0].get("customer_id") == 6, str(created6))

    print("== group=<id> valid: assigned to group ==")
    fx7 = FakeBackend()
    upd7 = await scenario_command(fx7, ["khanevade", "2", "group=2"], {})
    check("group assignment label", any("group company-x" in t for t in upd7.message.texts))

    print()
    if failures:
        print(f"RESULT: {len(failures)} FAILURES: {failures}")
        sys.exit(1)
    print("RESULT: all checks OK")


asyncio.run(main())
