"""Regression tests for delegate_bot's invite deep-link flow (2026-10-01):
/start dlgtok_<token> must claim the invite and land on the normal welcome +
menu, and every failure shape (404 expired/unknown, 409 already-linked,
server error) must produce its own polite text — never a fall-through to
NOT_A_DELEGATE. Telegram objects and the backend client are stubbed —
nothing real is touched.

Run (from delegate_bot/, using bot's venv — same interpreter family):
  ../bot/venv/Scripts/python test_invite_claim.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("DELEGATE_BOT_TOKEN", "")
os.environ.setdefault("DELEGATE_BOT_API_KEY", "k")
os.environ.setdefault("API_BASE_URL", "https://panel.test")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import texts  # noqa: E402
import handlers.delegate as delegate  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class StubMessage:
    def __init__(self):
        self.replies: list[tuple] = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append((text, reply_markup))


def make_update(args: list[str], user_id: int = 555, username: str = "ali_t") -> SimpleNamespace:
    msg = StubMessage()
    return SimpleNamespace(
        effective_message=msg,
        effective_user=SimpleNamespace(id=user_id, username=username),
        _context_args=args,
    ), msg


class StubBackend:
    def __init__(self):
        self.claim_calls: list[tuple[str, dict]] = []
        self.claim_result: dict = {"scope_name": "Ali", "default_duration_days": 30,
                                   "quick_volumes_gb": [10, 20]}
        self.claim_error = None

    async def post(self, path: str, json=None):
        if path == "/api/delegate/bot/claim":
            self.claim_calls.append((path, json))
            if self.claim_error is not None:
                raise delegate.DelegateApiError(self.claim_error[0], self.claim_error[1])
            return dict(self.claim_result)
        raise AssertionError(path)


async def fake_session(update) -> dict | None:
    return {"scope_name": "Ali", "default_duration_days": 30, "quick_volumes_gb": [10]}


stub = StubBackend()
delegate.backend = stub
session_calls: list = []


async def recording_session(update):
    session_calls.append(update)
    return await fake_session(update)


delegate._session = recording_session  # type: ignore[assignment]


async def main() -> None:
    # 1) happy path: the deep link claims the invite and lands on welcome+menu.
    upd, msg = make_update(["dlgtok_abc123"])
    await delegate.start(upd, SimpleNamespace(args=["dlgtok_abc123"]))
    check("claim POSTed with the stripped token, telegram_id and username",
          stub.claim_calls == [("/api/delegate/bot/claim", {
              "token": "abc123", "telegram_id": 555, "telegram_username": "ali_t"})])
    check("success shows the normal welcome for the claimed scope and the menu",
          len(msg.replies) == 1 and "Ali" in msg.replies[0][0] and msg.replies[0][1] is not None)

    # 2) 404 (unknown / used / expired token): dedicated text, no menu, and
    #    crucially NOT NOT_A_DELEGATE.
    stub.claim_error = ("invalid", 404)
    upd, msg = make_update(["dlgtok_dead"])
    await delegate.start(upd, SimpleNamespace(args=["dlgtok_dead"]))
    check("404 shows INVITE_INVALID_OR_EXPIRED",
          msg.replies[0][0] == texts.INVITE_INVALID_OR_EXPIRED)
    check("404 attaches no menu and never says NOT_A_DELEGATE",
          msg.replies[0][1] is None and msg.replies[0][0] != texts.NOT_A_DELEGATE)

    # 3) 409 (this Telegram account already linked somewhere else).
    stub.claim_error = ("linked", 409)
    upd, msg = make_update(["dlgtok_dup"])
    await delegate.start(upd, SimpleNamespace(args=["dlgtok_dup"]))
    check("409 shows INVITE_ALREADY_USED", msg.replies[0][0] == texts.INVITE_ALREADY_USED)

    # 4) a genuine server error stays GENERIC_ERROR (not an invite text).
    stub.claim_error = ("boom", 500)
    upd, msg = make_update(["dlgtok_x"])
    await delegate.start(upd, SimpleNamespace(args=["dlgtok_x"]))
    check("500 shows GENERIC_ERROR", msg.replies[0][0] == texts.GENERIC_ERROR)
    stub.claim_error = None

    # 5) payload-less /start: unchanged legacy behavior (session path), no
    #    claim POST, no _session call for the dlgtok case either.
    claims_before, sessions_before = len(stub.claim_calls), len(session_calls)
    upd, msg = make_update([])
    await delegate.start(upd, SimpleNamespace(args=[]))
    check("payload-less /start uses the session path exactly as before",
          len(stub.claim_calls) == claims_before and len(session_calls) == sessions_before + 1
          and msg.replies[0][0].startswith("سلام"))

    # 6) a non-dlgtok payload does not trigger the claim path either.
    upd, msg = make_update(["something-else"])
    await delegate.start(upd, SimpleNamespace(args=["something-else"]))
    check("non-dlgtok argument never reaches the claim endpoint",
          len(stub.claim_calls) == claims_before and len(session_calls) == sessions_before + 2)

    # 7) /start dlgtok_ (empty token after the prefix) still gets a polite
    #    404 text from the backend — not a crash, not NOT_A_DELEGATE.
    stub.claim_error = ("invalid", 404)
    upd, msg = make_update(["dlgtok_"])
    await delegate.start(upd, SimpleNamespace(args=["dlgtok_"]))
    check("bare dlgtok_ prefix is sent to the backend and refused politely (404 text)",
          len(stub.claim_calls) == claims_before + 1
          and msg.replies[0][0] == texts.INVITE_INVALID_OR_EXPIRED)


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All delegate-bot invite-claim cases passed.")
