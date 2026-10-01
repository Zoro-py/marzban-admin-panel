"""Behavioral tests for the shop-link admin commands (2026-10-01):
/shoplink (fuzzy resolve + forwardable Persian block + operator note) and
/shoplink_off (unlink / discard via DELETE). Telegram objects and the backend
client are mocked — nothing is created anywhere real. Mirror of
test_delegate_invite.py, which tests the delegate pair the same way.

Run from bot/:  python test_shop_link.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

os.environ["ADMIN_CHAT_ID"] = "777"  # hard-set: CI exports its own value and the admin gate compares against it (setdefault would lose)
os.environ.setdefault("API_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import handlers.common as common  # noqa: E402
import handlers.shop_link_admin as sla  # noqa: E402

failures: list[str] = []
ADMIN = 777


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class FakeBackend:
    def __init__(self):
        self.customers = [{"id": 5, "name": "Ali"}, {"id": 6, "name": "Alireza"}]
        self.next_id = 1
        self.posts: list[tuple[str, dict]] = []
        self.deletes: list[str] = []
        self.fail_delete_msg: str | None = None

    async def get(self, url: str, params=None):
        if url == "/api/customers":
            return self.customers
        raise AssertionError(f"unexpected GET {url}")

    async def post(self, url: str, json=None, timeout: float = 20):
        self.posts.append((url, json or {}))
        if url == "/api/shop/link-invite":
            name = next(c["name"] for c in self.customers if c["id"] == json["customer_id"])
            return {
                "customer_id": json["customer_id"],
                "customer_name": name,
                "invite_url": f"https://t.me/shop_bot?start=shoplnk_tok{self.next_id}",
                "claim_expires_at": "2026-10-08T12:00:00",
            }
        raise AssertionError(f"unexpected POST {url}")

    async def delete(self, url: str):
        self.deletes.append(url)
        if self.fail_delete_msg:
            raise ValueError(self.fail_delete_msg)
        return {"ok": True}


def make_update() -> tuple[SimpleNamespace, list[str]]:
    replies: list[str] = []

    class Msg:
        async def reply_text(self, text, **kwargs):
            replies.append(text)

    return (SimpleNamespace(effective_chat=SimpleNamespace(id=ADMIN), message=Msg()), replies)


async def main() -> None:
    backend = FakeBackend()
    sla.backend = backend
    common.backend = backend  # resolve_customer rides common's own reference

    # ── /shoplink ──────────────────────────────────────────────────────────
    upd, replies = make_update()
    await sla.shoplink_command(upd, SimpleNamespace(args=[]))
    check("no args shows a usage line", any("Usage:" in r for r in replies) and not backend.posts)

    upd, replies = make_update()
    await sla.shoplink_command(upd, SimpleNamespace(args=["al"]))
    # "al" substring-matches both Ali and Alireza; resolve_customer raises
    # AmbiguousMatch for it (an exact match would resolve outright instead).
    check("ambiguous name lists the candidates and posts nothing",
          any("Alireza" in r and "#6" in r for r in replies) and not backend.posts)

    upd, replies = make_update()
    await sla.shoplink_command(upd, SimpleNamespace(args=["5"]))
    check("numeric id resolves and posts the invite",
          backend.posts == [("/api/shop/link-invite", {"customer_id": 5})])
    url = "https://t.me/shop_bot?start=shoplnk_tok1"
    check("the forward-ready block contains the customer name, «۷ روز» and the link on its own line",
          len(replies) == 2 and "Ali" in replies[0] and "۷ روز" in replies[0] and url in replies[0].splitlines())
    check("the operator note names /shoplink_off and the customer id",
          "/shoplink_off 5" in replies[1] and "Single-use" in replies[1])

    # ── /shoplink_off ──────────────────────────────────────────────────────
    upd, replies = make_update()
    await sla.shoplink_off_command(upd, SimpleNamespace(args=[]))
    check("off without args shows usage", any("Usage:" in r for r in replies) and not backend.deletes)

    upd, replies = make_update()
    await sla.shoplink_off_command(upd, SimpleNamespace(args=["5"]))
    check("off calls DELETE on the customer's link endpoint and confirms",
          backend.deletes == ["/api/shop/link/5"] and any("removed" in r for r in replies))

    backend.fail_delete_msg = "This customer has pay-as-you-go billing: the monthly settle keeps charging their ledger"
    upd, replies = make_update()
    await sla.shoplink_off_command(upd, SimpleNamespace(args=["6"]))
    check("a backend refusal surfaces its readable detail",
          any("pay-as-you-go" in r for r in replies))
    backend.fail_delete_msg = None


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All shop-link admin-command cases passed.")
