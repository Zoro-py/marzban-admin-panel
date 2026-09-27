"""DEL-1 regression (2026-09-27 audit): the accounts list survives a Telegram
429 RetryAfter mid-burst. Before the fix, _list_accounts' loop let RetryAfter
propagate — the list died halfway and on_error reported a generic error, so
the delegate never saw the remaining accounts. Green: the burst helper waits
retry_after + 1s and the line still lands. Telegram objects are stubbed —
nothing real is touched.

Run (from delegate_bot/):
  python test_list_flood_guard.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest.mock as mock
from types import SimpleNamespace

os.environ.setdefault("DELEGATE_BOT_TOKEN", "")
os.environ.setdefault("DELEGATE_BOT_API_KEY", "k")
os.environ.setdefault("API_BASE_URL", "https://panel.test")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telegram.error import RetryAfter  # noqa: E402

import handlers.delegate as delegate  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class FlakyMessage:
    """Fails the first `fail_left` sends with RetryAfter(n), then succeeds."""

    def __init__(self, fail_left: int):
        self.fail_left = fail_left
        self.calls: list[str] = []

    async def reply_text(self, text, **kwargs):
        if self.fail_left:
            self.fail_left -= 1
            raise RetryAfter(2)
        self.calls.append(text)


async def main() -> None:
    # 1. one 429 → one polite wait → the line still lands
    msg = FlakyMessage(fail_left=1)
    sleeps: list[float] = []
    with mock.patch.object(delegate.asyncio, "sleep", new=mock.AsyncMock(side_effect=sleeps.append)):
        await delegate._send_list_line(msg, "account line", reply_markup=SimpleNamespace())
    check("line delivered after one 429", msg.calls == ["account line"], f"calls={msg.calls}")
    check("waited retry_after+1", sleeps == [3.0], f"sleeps={sleeps}")

    # 2. no 429 → no sleep, single send (the helper adds nothing to the happy path)
    msg2 = FlakyMessage(fail_left=0)
    sleeps2: list[float] = []
    with mock.patch.object(delegate.asyncio, "sleep", new=mock.AsyncMock(side_effect=sleeps2.append)):
        await delegate._send_list_line(msg2, "second line")
    check("clean path: one send, no sleep", msg2.calls == ["second line"] and sleeps2 == [])

    # 3. the burst path actually uses the helper (DEL-1 was specifically the list loop)
    import inspect
    src = inspect.getsource(delegate._list_accounts)
    check("_list_accounts sends via _send_list_line", "_send_list_line" in src)

    if failures:
        print(f"\nFAILED: {len(failures)}: {failures}")
        sys.exit(1)
    print("\nall flood-guard checks passed")


if __name__ == "__main__":
    asyncio.run(main())
