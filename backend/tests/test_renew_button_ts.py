"""M-2 (2026-11-17): the warning sweep's renew buttons carry their creation
timestamp, via one helper both the trial and the real-account branch use.

The shopbot refuses taps older than its own _RENEW_BUTTON_MAX_AGE_S (7
days) by reading the ts out of the callback data; buttons from before the
timestamp (old chats, the old-backend deploy window) carry none and keep
working — compat, deliberate. Nothing about pricing or volume changes:
the helper only shapes the callback string.

Plain `python -m tests.test_renew_button_ts` from `backend/`.
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="renew_ts_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""
os.environ["SHOP_BOT_API_KEY"] = "test-shop-key"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import shop_service  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


def main() -> None:
    check("shop_service.warning_renew_callback exists",
          callable(getattr(shop_service, "warning_renew_callback", None)),
          "AttributeError on base")

    fn = shop_service.warning_renew_callback
    now = int(time.time())

    trial = fn(is_trial=True, account_id=None, renew_gb=10)
    parts = trial.split(":")
    check("trial button: renew:<gb>:<ts>, three segments",
          len(parts) == 3 and parts[0] == "renew" and parts[1] == "10", repr(trial))
    check("trial button ts is fresh",
          len(parts) == 3 and abs(int(parts[2]) - now) < 120, repr(trial))

    real = fn(is_trial=False, account_id=42, renew_gb=35.5)
    parts = real.split(":")
    check("real-account button: ren:<id>:<gb>:<ts>, four segments",
          len(parts) == 4 and parts[0] == "ren" and parts[1] == "42" and parts[2] == "35.5",
          repr(real))
    check("real-account button ts is fresh",
          len(parts) == 4 and abs(int(parts[3]) - now) < 120, repr(real))

    src = inspect.getsource(shop_service)
    check("the sweep builds its renew button through the helper",
          "warning_renew_callback(" in src
          and 'callback_data": f"renew:' not in src
          and 'callback_data": f"ren:' not in src)


main()

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All renew-button-ts cases passed.")
