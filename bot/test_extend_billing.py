"""Behavioral regression test for bot /extend's billing order (MONEY-ADJ-1):
the GB charge must be posted to /api/ledger BEFORE the /adjust call, and the
adjust must carry bill_added_gb=true — otherwise the added GB shows as
pending too and the next settle bills it a second time (the double-charge
that hit account 39 for 300,000 T). Telegram objects and the backend client
are mocked — the charge is CAPTURED, never posted.

F13 (2026-10-23) added the Confirm/Cancel step and Idempotency-Key headers,
so the flow under test is now: /extend plans and shows a confirm card (and
posts NOTHING), extend_callback does the charge → adjust pair on Confirm.
The same billing-order contract is asserted through that flow, plus: no
posts before Confirm, the two idempotency keys, the cancel path, the
double-tap guard, and the charge-failed → adjust-still-sent-without-flag
safety direction.

Run from bot/:  venv/Scripts/python test_extend_billing.py
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
os.environ.setdefault("BOT_TOKEN", "")
os.environ.setdefault("SHOP_BOT_TOKEN", "")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import handlers.account as account  # noqa: E402
import handlers.common as common  # noqa: E402

failures: list[str] = []
ADMIN = 777


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


class FakeBackend:
    """Captures posts in order (url, json, idempotency_key) so the
    charge-before-adjust contract is assertable, not assumed."""

    def __init__(self):
        self.posts: list[tuple[str, dict, str | None]] = []
        self.fail_ledger = False

    async def get(self, url: str):
        if url == "/api/accounts":
            return [{"id": 9, "marzban_username": "acc1", "customer_id": 5,
                     "effective_rate": 5000.0, "rate_configured": True}]
        raise AssertionError(f"unexpected GET {url}")

    async def post(self, url: str, json=None, idempotency_key: str | None = None):
        if url == "/api/ledger" and self.fail_ledger:
            raise ValueError("ledger exploded")
        self.posts.append((url, json or {}, idempotency_key))
        if url.endswith("/adjust"):
            return {"marzban_username": "acc1", "expire": 9999999999, "data_limit": 40 * 1024**3}
        return {"amount": (json or {}).get("amount")}


fake = FakeBackend()
account.backend = fake
common.backend = fake  # resolve_account reads through handlers.common.backend


def make_update(args: list[str], update_id: int = 4242):
    replies: list[str] = []
    sent_markups: list[object] = []

    async def _reply(text, **kwargs):
        replies.append(text)
        sent_markups.append(kwargs.get("reply_markup"))

    message = SimpleNamespace(reply_text=_reply)
    upd = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=ADMIN),
                          effective_user=SimpleNamespace(id=ADMIN), update_id=update_id)
    ctx = SimpleNamespace(args=args)
    return upd, ctx, replies, sent_markups


def make_callback(token: int, action: str = "ok", card_text: str = "card"):
    edits: list[str] = []

    async def _edit(text, **kwargs):
        edits.append(text)

    async def _answer():
        return None

    query = SimpleNamespace(
        data=f"extend:{action}:{token}",
        message=SimpleNamespace(text=card_text),
        edit_message_text=_edit,
        answer=_answer,
    )
    upd = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=ADMIN),
                          effective_user=SimpleNamespace(id=ADMIN))
    return upd, edits


async def confirm_pending(card_text: str, action: str = "ok", update_id: int = 4242):
    """Clicks the button on the one pending confirmation; returns the edits."""
    assert len(account._pending_extend) == 1, "exactly one pending extend expected"
    token = next(iter(account._pending_extend))
    upd, edits = make_callback(token, action=action, card_text=card_text)
    await account.extend_callback(upd, SimpleNamespace())
    return edits


async def main() -> None:
    # 0) planning posts NOTHING: /extend only shows the confirm card.
    upd, ctx, replies, markups = make_update(["acc1", "30", "10"])
    await account.extend_command(upd, ctx)
    check("command alone posts nothing (charge and adjust wait for Confirm)",
          fake.posts == [])
    check("confirm card shows the charge, the deltas and the Confirm question",
          "Will charge 50,000 T" in replies[0] and "+30 day(s)" in replies[0]
          and "+10 GB" in replies[0] and replies[0].rstrip().endswith("Confirm?"))
    check("card carries Confirm/Cancel buttons on the extend: prefix",
          markups[0] is not None)

    # 1) the billing path: Confirm → charge FIRST, then adjust carrying
    #    bill_added_gb=true, both keyed to the /extend update.
    edits = await confirm_pending(replies[0])
    urls = [u for u, _, _ in fake.posts]
    check("charge posted before adjust (order matters for the double-bill fix)",
          urls == ["/api/ledger", "/api/accounts/9/adjust"])
    check("charge is the GB charge at the effective rate",
          fake.posts[0][1]["amount"] == 50000.0 and fake.posts[0][1]["type"] == "charge")
    check("adjust carries bill_added_gb=true", fake.posts[1][1].get("bill_added_gb") is True)
    check("adjust still carries the size delta", fake.posts[1][1].get("extend_gb") == 10)
    check("ledger POST carries tg-extend-<update_id> idempotency key",
          fake.posts[0][2] == "tg-extend-4242")
    check("adjust POST carries its own tg-extend-adjust-<update_id> key",
          fake.posts[1][2] == "tg-extend-adjust-4242")
    check("operator told the amount was charged", any("charged" in e for e in edits))

    # 1b) double-tap / redelivery of the same button: pending was popped, so
    #     nothing can be posted twice.
    upd2, edits2 = make_callback(0, action="ok", card_text="card")
    await account.extend_callback(upd2, SimpleNamespace())
    check("second tap on a used button refuses and posts nothing",
          len(fake.posts) == 2 and any("already expired or was used" in e for e in edits2))

    # 2) days-only: never billed, no flag, adjust key still sent
    fake.posts.clear()
    upd, ctx, replies, _ = make_update(["acc1", "15"], update_id=51)
    await account.extend_command(upd, ctx)
    check("days-only card announces no GB charge",
          "No GB charge" in replies[0])
    await confirm_pending(replies[0], update_id=51)
    check("days-only adjust posts no charge and no flag",
          len(fake.posts) == 1 and fake.posts[0][0].endswith("/adjust")
          and "bill_added_gb" not in fake.posts[0][1])
    check("days-only adjust still carries its idempotency key",
          fake.posts[0][2] == "tg-extend-adjust-51")

    # 3) unassigned account: adjust without billing (unchanged semantics)
    fake.posts.clear()

    async def _get_unassigned(url):
        if url == "/api/accounts":
            return [{"id": 10, "marzban_username": "lonely", "customer_id": None, "group_id": None,
                     "effective_rate": 5000.0, "rate_configured": True}]
        raise AssertionError(url)

    fake.get = _get_unassigned  # type: ignore[method-assign]
    upd, ctx, replies, _ = make_update(["lonely", "30", "5"], update_id=61)
    await account.extend_command(upd, ctx)
    check("unassigned card says why it is not billed",
          "Not billed — unassigned account" in replies[0])
    await confirm_pending(replies[0], update_id=61)
    check("unassigned account: adjust only, no charge, no flag",
          len(fake.posts) == 1 and fake.posts[0][0].endswith("/adjust")
          and "bill_added_gb" not in fake.posts[0][1])
    check("unassigned is said out loud", any("unassigned" in r for r in replies))

    # 4) Cancel: the card's whole point — nothing posted, pending consumed.
    fake.posts.clear()

    async def _get_acc1(url):
        if url == "/api/accounts":
            return [{"id": 9, "marzban_username": "acc1", "customer_id": 5,
                     "effective_rate": 5000.0, "rate_configured": True}]
        raise AssertionError(url)

    fake.get = _get_acc1  # type: ignore[method-assign]
    upd, ctx, replies, _ = make_update(["acc1", "30", "10"], update_id=71)
    await account.extend_command(upd, ctx)
    edits = await confirm_pending(replies[0], action="no", update_id=71)
    check("Cancel posts nothing and says so",
          fake.posts == [] and any("Cancelled" in e for e in edits))

    # 5) the ledger charge FAILS: adjust must still go out WITHOUT
    #    bill_added_gb (the GB stays pending and visible for the next settle
    #    instead of vanishing) and the operator must be told to bill by hand.
    fake.posts.clear()
    fake.fail_ledger = True
    upd, ctx, replies, _ = make_update(["acc1", "30", "10"], update_id=81)
    await account.extend_command(upd, ctx)
    edits = await confirm_pending(replies[0], update_id=81)
    fake.fail_ledger = False
    check("failed charge: adjust still sent, without the billed flag",
          [u for u, _, _ in fake.posts] == ["/api/accounts/9/adjust"]
          and "bill_added_gb" not in fake.posts[0][1])
    check("failed charge: operator told to bill by hand",
          any("NOT billed" in e for e in edits))


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All /extend billing-order cases passed.")
