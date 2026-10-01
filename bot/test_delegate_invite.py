"""Behavioral tests for the delegate invite-link admin commands (2026-10-01):
/delegate_invite (fuzzy resolve + forwardable Persian block + operator
note), /delegate_revoke (DELETE on a pending invite id), and
/delegate_invite_dump (one .txt line per pending invite). Telegram objects
and the backend client are mocked — nothing is created anywhere real.

Run from bot/:  venv/Scripts/python test_delegate_invite.py
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
from types import SimpleNamespace

os.environ["ADMIN_CHAT_ID"] = "777"  # hard-set: CI exports its own value and the admin gate compares against it (setdefault would lose)
os.environ.setdefault("API_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import handlers.common as common  # noqa: E402
import handlers.delegate_admin as da  # noqa: E402

failures: list[str] = []
ADMIN = 777


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class FakeBackend:
    def __init__(self):
        self.customers = [{"id": 5, "name": "Ali"}, {"id": 6, "name": "Alireza"}]
        self.invites: dict[int, dict] = {}
        self.next_id = 1
        self.posts: list[tuple[str, dict]] = []
        self.deletes: list[str] = []
        self.fail_delete = False

    async def get(self, url: str, params=None):
        if url == "/api/customers":
            return self.customers
        if url == "/api/delegate":
            return list(self.invites.values())
        raise AssertionError(f"unexpected GET {url}")

    async def post(self, url: str, json=None, timeout: float = 20):
        self.posts.append((url, json or {}))
        if url == "/api/delegate/invite":
            invite = {
                "id": self.next_id,
                "customer_id": json["customer_id"],
                "group_id": None,
                "scope_name": next(c["name"] for c in self.customers if c["id"] == json["customer_id"]),
                "telegram_id": None,
                "label": None,
                "is_active": False,
                "claim_expires_at": "2026-10-08T12:00:00",
                "invite_url": f"https://t.me/delegate_bot?start=dlgtok_tok{self.next_id}",
            }
            self.invites[self.next_id] = invite
            self.next_id += 1
            return invite
        raise AssertionError(f"unexpected POST {url}")

    async def delete(self, url: str):
        self.deletes.append(url)
        if self.fail_delete:
            raise ValueError("This delegate is already claimed — deactivate it instead")
        delegate_id = int(url.rsplit("/", 1)[1])
        self.invites.pop(delegate_id, None)
        return {"ok": True}


def make_update() -> tuple[SimpleNamespace, list[str]]:
    replies: list[str] = []

    class Msg:
        async def reply_text(self, text, **kwargs):
            replies.append(text)

    return (SimpleNamespace(effective_chat=SimpleNamespace(id=ADMIN), message=Msg()), replies)


async def main() -> None:
    backend = FakeBackend()
    da.backend = backend
    common.backend = backend  # resolve_customer rides common's own reference

    # ── /delegate_invite ──────────────────────────────────────────────────
    upd, replies = make_update()
    await da.delegate_invite_command(upd, SimpleNamespace(args=[]))
    check("no args shows a usage line", any("Usage:" in r for r in replies) and not backend.posts)

    upd, replies = make_update()
    await da.delegate_invite_command(upd, SimpleNamespace(args=["al"]))
    # "al" substring-matches both Ali and Alireza; resolve_customer raises
    # AmbiguousMatch for it (an exact match would resolve outright instead).
    check("ambiguous name lists the candidates and posts nothing",
          any("Alireza" in r and "#6" in r for r in replies) and not backend.posts)

    upd, replies = make_update()
    await da.delegate_invite_command(upd, SimpleNamespace(args=["5"]))
    check("numeric id resolves and posts the invite", backend.posts == [("/api/delegate/invite", {"customer_id": 5})])
    url = backend.invites[1]["invite_url"]
    check("the forward-ready block contains the customer name, the expiry «۷ روز» and the link on its own line",
          len(replies) == 2 and "Ali" in replies[0] and "۷ روز" in replies[0] and url in replies[0].splitlines())
    check("the operator note names the invite id and /delegate_revoke",
          f"/delegate_revoke {backend.invites[1]['id']}" in replies[1] and "Single-use" in replies[1])

    # ── /delegate_revoke ──────────────────────────────────────────────────
    upd, replies = make_update()
    await da.delegate_revoke_command(upd, SimpleNamespace(args=[]))
    check("revoke without args shows usage", any("Usage:" in r for r in replies))

    upd, replies = make_update()
    await da.delegate_revoke_command(upd, SimpleNamespace(args=["abc"]))
    check("revoke with a non-numeric id shows usage", any("Usage:" in r for r in replies) and not backend.deletes)

    upd, replies = make_update()
    await da.delegate_revoke_command(upd, SimpleNamespace(args=["1"]))
    check("revoke calls DELETE on the invite id and confirms in one line",
          backend.deletes == ["/api/delegate/1"] and any("deleted" in r for r in replies))

    backend.fail_delete = True
    upd, replies = make_update()
    await da.delegate_revoke_command(upd, SimpleNamespace(args=["2"]))
    check("revoke on a claimed delegate surfaces the backend's 409 detail",
          any("already claimed" in r for r in replies))
    backend.fail_delete = False

    # ── /delegate_invite_dump ─────────────────────────────────────────────
    documents: list[tuple[str, bytes]] = []

    class Bot:
        async def send_document(self, chat_id=None, document=None, filename=None):
            buf = io.BytesIO(document.read()) if hasattr(document, "read") else None
            documents.append((filename, buf.getvalue() if buf else b""))

    context = SimpleNamespace(bot=Bot(), args=[])
    upd, replies = make_update()
    await da.delegate_invite_dump_command(upd, context)
    check("dump with no pending invites says so", any("No pending invites" in r for r in replies) and not documents)

    await backend.post("/api/delegate/invite", json={"customer_id": 6})
    upd, replies = make_update()
    await da.delegate_invite_dump_command(upd, context)
    check("dump sends exactly one document", len(documents) == 1)
    filename, content = documents[0]
    check("dump filename has the expected shape", filename.startswith("delegate_invites_") and filename.endswith(".txt"))
    lines = content.decode("utf-8").strip().splitlines()
    check("dump line format: id | scope | link | ISO date",
          len(lines) == 1 and lines[0].startswith(f"#{backend.invites[2]['id']} | Alireza | ")
          and "https://t.me/delegate_bot?start=dlgtok_" in lines[0]
          and "expires 2026-10-08" in lines[0])
    check("a count line follows the document", any("pending invite" in r for r in replies))


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All delegate invite admin-command cases passed.")
