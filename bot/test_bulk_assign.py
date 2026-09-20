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
    check("the bot no longer creates the customer itself (backend owns the family default)",
          all(u != "/api/customers" for u, _ in fx5.posts))
    check("batch posted with NO owner and NOT unassigned → backend attaches the family",
          len(created) == 1 and "customer_id" not in created[0] and not created[0].get("unassigned"), str(created))
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
    check("bot creates no customer (backend reuses the same-named one)",
          all(u != "/api/customers" for u, _ in fx6.posts))
    check("batch posted owner-less for the backend default",
          len(created6) == 1 and "customer_id" not in created6[0] and not created6[0].get("unassigned"), str(created6))

    print("== «without owner» button: explicit opt-out is sent as unassigned=True ==")
    fx8 = FakeBackend()
    user_data8: dict = {}
    upd8 = await scenario_command(fx8, ["khanevade", "2"], user_data8)
    token8 = [c for c in callbacks(upd8.message.markups[0]) if c.startswith("bulk:go:")][0].split(":")[2]
    q8 = FakeQuery(f"bulk:go:{token8}")
    await bulk.bulk_callback(SimpleNamespace(callback_query=q8, effective_user=SimpleNamespace(id=ADMIN),
                                             effective_chat=SimpleNamespace(id=ADMIN)),
                             SimpleNamespace(user_data=user_data8))
    created8 = [b for u, b in fx8.posts if u == "/api/accounts/bulk"]
    check("go with no owner posts unassigned=True", len(created8) == 1 and created8[0].get("unassigned") is True, str(created8))

    print("== assigned «go»: customer_id kept, no unassigned flag ==")
    fx9 = FakeBackend()
    user_data9: dict = {}
    upd9 = await scenario_command(fx9, ["khanevade", "2", "cust=Ali"], user_data9)
    token9 = callbacks(upd9.message.markups[0])[0].split(":")[2]
    q9 = FakeQuery(f"bulk:go:{token9}")
    await bulk.bulk_callback(SimpleNamespace(callback_query=q9, effective_user=SimpleNamespace(id=ADMIN),
                                             effective_chat=SimpleNamespace(id=ADMIN)),
                             SimpleNamespace(user_data=user_data9))
    created9 = [b for u, b in fx9.posts if u == "/api/accounts/bulk"]
    check("customer_id 5 kept, not unassigned", len(created9) == 1 and created9[0].get("customer_id") == 5 and not created9[0].get("unassigned"), str(created9))

    print("== group=<id> valid: assigned to group ==")
    fx7 = FakeBackend()
    upd7 = await scenario_command(fx7, ["khanevade", "2", "group=2"], {})
    check("group assignment label", any("group company-x" in t for t in upd7.message.texts))

    print("== unknown callback action: refused, batch NOT created, still pending ==")
    fx10 = FakeBackend()
    user_data10: dict = {}
    upd10 = await scenario_command(fx10, ["khanevade", "2"], user_data10)
    token10 = callbacks(upd10.message.markups[0])[0].split(":")[2]
    q10 = FakeQuery(f"bulk:weird:{token10}")
    await bulk.bulk_callback(SimpleNamespace(callback_query=q10, effective_user=SimpleNamespace(id=ADMIN),
                                             effective_chat=SimpleNamespace(id=ADMIN)),
                             SimpleNamespace(user_data=user_data10))
    check("nothing posted to /api/accounts/bulk", all(u != "/api/accounts/bulk" for u, _ in fx10.posts))
    check("operator told the button isn't recognised", any("isn't recognised" in e for e in q10.edits))
    check("the pending batch is still there to confirm", bool(user_data10.get(bulk._PENDING_KEY)))

    print("== warnings from the backend are shown to the operator ==")
    fx11 = FakeBackend()
    async def _post_with_warning(url, json=None, timeout=20):
        if url == "/api/accounts/bulk":
            return {"created": 2, "skipped": 0, "failed": 0, "items": [], "notifications_queued": True,
                    "warnings": ["The family customer could not be set up, so 2 account(s) were created WITHOUT an owner."]}
        return await FakeBackend.post(fx11, url, json=json, timeout=timeout)
    fx11.post = _post_with_warning
    user_data11: dict = {}
    upd11 = await scenario_command(fx11, ["khanevade", "2"], user_data11)
    token11 = callbacks(upd11.message.markups[0])[0].split(":")[2]
    q11 = FakeQuery(f"bulk:asnew:{token11}")
    await bulk.bulk_callback(SimpleNamespace(callback_query=q11, effective_user=SimpleNamespace(id=ADMIN),
                                             effective_chat=SimpleNamespace(id=ADMIN)),
                             SimpleNamespace(user_data=user_data11))
    check("warning text reaches the final message", any("WITHOUT an owner" in e for e in q11.edits), str(q11.edits))

    print()
    if failures:
        print(f"RESULT: {len(failures)} FAILURES: {failures}")
        sys.exit(1)
    print("RESULT: all checks OK")


asyncio.run(main())
