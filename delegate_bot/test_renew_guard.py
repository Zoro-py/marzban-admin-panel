"""Regression tests for delegate_bot's conversation guards — locks the fixes
from commits 3369200 (double-tap renew could double-charge) and efe410c
(stale inline keyboards left behind after a flow resolves), which previously
had NO automated test. Telegram objects and the backend client are stubbed —
nothing real is touched.

Run (from delegate_bot/, using bot's venv — same interpreter family):
  ../bot/venv/Scripts/python test_renew_guard.py
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

import handlers.delegate as delegate  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class StubQuery:
    def __init__(self, data: str):
        self.data = data
        self.message = SimpleNamespace(message_id=77, text="🟢 d1 — 0 از 10 گیگ — ۳۰ روز مانده")
        self.edits: list[tuple] = []
        self.answered = False

    async def answer(self) -> None:
        self.answered = True

    async def edit_message_text(self, text, reply_markup=...):
        self.edits.append((text, reply_markup))


def make_update(data: str, user_id: int = 555) -> SimpleNamespace:
    q = StubQuery(data)
    return SimpleNamespace(
        callback_query=q,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=None,
    )


class StubBackend:
    def __init__(self):
        self.renew_calls = 0
        self.create_calls = 0
        self.in_flight_delay = 0.0
        self.fail_next = False

    async def post(self, path: str, json=None):
        if self.fail_next:
            self.fail_next = False
            raise delegate.DelegateApiError("boom", 500)
        if path.endswith("/renew"):
            self.renew_calls += 1
            if self.in_flight_delay:
                await asyncio.sleep(self.in_flight_delay)
            return {"marzban_username": "d1"}
        if path.endswith("/accounts"):
            self.create_calls += 1
            if self.in_flight_delay:
                await asyncio.sleep(self.in_flight_delay)
            return {"marzban_username": "d2"}
        raise AssertionError(path)


async def fake_session(update) -> dict:
    return {"scope_name": "x", "default_duration_days": 30, "quick_volumes_gb": [10, 20]}


stub = StubBackend()
delegate.backend = stub
delegate._session = fake_session  # type: ignore[assignment]


async def main() -> None:
    # 1) 3369200's blocking bug: two CONCURRENT taps on the same renew button
    #    must reach the backend exactly once (modify_user is an absolute SET,
    #    so a second concurrent call would charge again for one extension).
    stub.renew_calls = 0
    stub.in_flight_delay = 0.05
    upd1, upd2 = make_update("d:renew:123:10"), make_update("d:renew:123:10")
    await asyncio.gather(delegate.handle_callback(upd1, SimpleNamespace()),
                         delegate.handle_callback(upd2, SimpleNamespace()))
    check("concurrent double-tap on renew reaches the backend once", stub.renew_calls == 1,
          f"calls={stub.renew_calls}")
    check("the guarded (second) tap gets no edit — its message is already being resolved",
          len(upd2.callback_query.edits) == 0)
    stub.in_flight_delay = 0.0

    # 2) the guard is released after the flow settles: a LATER genuine renew
    #    of the same account+volume still goes through.
    upd3 = make_update("d:renew:123:10")
    await delegate.handle_callback(upd3, SimpleNamespace())
    check("after the first flow settles, a new tap renews again", stub.renew_calls == 2)

    # 3) 3369200 double-charge shape, backend-side variant: a failing renew
    #    does not wedge the guard (popped in finally) — the retry goes through.
    stub.renew_calls = 0
    stub.fail_next = True
    upd4 = make_update("d:renew:123:10")
    await delegate.handle_callback(upd4, SimpleNamespace())
    check("failed renew reports the backend's 400 text verbatim", stub.renew_calls == 0
          and any("boom" in (t or "") for t, _ in upd4.callback_query.edits) is False)  # 500 -> GENERIC_ERROR
    upd5 = make_update("d:renew:123:10")
    await delegate.handle_callback(upd5, SimpleNamespace())
    check("retry after failure is not wedged by the guard", stub.renew_calls == 1)

    # 4) efe410c: a resolved flow clears its stale inline keyboard — the
    #    success and error edits must both pass reply_markup=None explicitly.
    upd6 = make_update("d:new:10")
    await delegate.handle_callback(upd6, SimpleNamespace())
    check("create success clears the volume picker keyboard",
          upd6.callback_query.edits and upd6.callback_query.edits[-1][1] is None,
          f"markup={upd6.callback_query.edits[-1][1] if upd6.callback_query.edits else None}")
    stub.fail_next = True
    upd7 = make_update("d:new:10")
    await delegate.handle_callback(upd7, SimpleNamespace())
    check("create failure ALSO clears the keyboard (stale buttons are the trap)",
          upd7.callback_query.edits and upd7.callback_query.edits[-1][1] is None,
          f"markup={upd7.callback_query.edits[-1][1] if upd7.callback_query.edits else None}")


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All delegate-bot guard cases passed.")
