"""warn_delegates_before_service_ends (F15): a delegate is DM'd 3 days before
an account's expiry or at >=80% usage — once per account per episode, and
only a DELIVERED warning is recorded, so a failed send retries on a later
pass instead of the delegate silently never hearing.

Plain `python tests/test_delegate_lifecycle_warnings.py` from `backend/`.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time as _time
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="delegate_warn_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select  # noqa: E402

from app import notify as notify_module  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.delegate_service import warn_delegates_before_service_ends  # noqa: E402
from app.models import Account, AccountEvent, Customer, Delegate  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual == expected:
        print(f"[OK] {label}")
    else:
        print(f"[FAIL] {label}\n       expected: {expected!r}\n       actual:   {actual!r}")
        failures.append(label)


with Session(engine) as s:
    c = Customer(name="Delegate Warn Probe")
    s.add(c)
    s.commit()
    s.refresh(c)
    cid = c.id
    d = Delegate(customer_id=cid, telegram_id=4242, is_active=True, label="probe")
    s.add(d)
    s.commit()
    s.refresh(d)
    acct = Account(marzban_username="del_warn_acct", customer_id=cid, status="active",
                   expire=int(_time.time()) + 20 * 86400)
    s.add(acct)
    s.commit()
    s.refresh(acct)
    aid = acct.id


def events(action: str) -> list[AccountEvent]:
    with Session(engine) as s:
        return s.exec(select(AccountEvent).where(
            AccountEvent.account_id == aid,
            AccountEvent.action == action,
        ).order_by(AccountEvent.id)).all()


def set_account(**fields) -> None:
    with Session(engine) as s:
        acct = s.get(Account, aid)
        for k, v in fields.items():
            setattr(acct, k, v)
        s.add(acct)
        s.commit()


def run_pass() -> int:
    with Session(engine) as s:
        return asyncio.run(warn_delegates_before_service_ends(s))


SENT: list[tuple[int, str]] = []


async def capture(chat_id, text):
    SENT.append((chat_id, text))


async def broken(chat_id, text):
    raise RuntimeError("telegram down")


original_send = notify_module.send_to_delegate
notify_module.send_to_delegate = capture
try:
    # healthy account -> nothing
    check("healthy account: nothing sent", run_pass(), 0)
    check("no event recorded", len(events("delegate_expiry_warned")), 0)

    # expiry within 3 days -> one warning, recorded, once per episode
    set_account(expire=int(_time.time()) + 2 * 86400)
    check("expiry 2 days out: sent", run_pass(), 1)
    check("delivered warning recorded as an AccountEvent",
          len(events("delegate_expiry_warned")), 1)
    check("it went to the delegate's telegram id", SENT[-1][0], 4242)
    check("it names the account", "del_warn_acct" in SENT[-1][1], True)
    check("it says renewal is the delegate's call", "تمدید" in SENT[-1][1], True)
    check("same episode: never twice", run_pass(), 0)

    # renewal moves expire out, then decay back in -> a NEW episode warns again
    set_account(expire=int(_time.time()) + 20 * 86400)
    check("renewed: quiet again", run_pass(), 0)
    set_account(expire=int(_time.time()) + 86400)
    check("decayed back into the window: a new episode warns", run_pass(), 1)

    # usage at 90% -> the usage warning, once per episode
    set_account(expire=int(_time.time()) + 20 * 86400,
                data_limit=10 * 1024 ** 3, used_traffic=9 * 1024 ** 3)
    check("usage 90%: sent", run_pass(), 1)
    check("usage warning recorded", len(events("delegate_usage_warned")), 1)
    check("usage same episode: never twice", run_pass(), 0)

    # renewal raises the limit -> percent drops, quiet
    set_account(data_limit=20 * 1024 ** 3)
    check("renewed volume: quiet", run_pass(), 0)

    # a FAILED send is NOT recorded — the next pass retries. Fresh episode:
    # a limit value this account has never been warned at.
    set_account(data_limit=11 * 1024 ** 3)  # 9/11 = 81.8%
    markers_before = len(events("delegate_usage_warned"))
    notify_module.send_to_delegate = broken
    check("failed send counts as not sent", run_pass(), 0)
    notify_module.send_to_delegate = capture
    check("...and leaves no marker behind",
          len(events("delegate_usage_warned")), markers_before)
    check("next pass tries again and delivers", run_pass(), 1)

    # an INACTIVE delegate is never DM'd, even with an expiring account
    with Session(engine) as s:
        dlg = s.exec(select(Delegate).where(Delegate.customer_id == cid)).one()
        dlg.is_active = False
        s.add(dlg)
        s.commit()
    set_account(expire=int(_time.time()) + 86400,
                data_limit=12 * 1024 ** 3, used_traffic=11 * 1024 ** 3)
    before = len(SENT)
    check("inactive delegate: silent", run_pass(), 0)
    check("no message went out", len(SENT), before)
finally:
    notify_module.send_to_delegate = original_send

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All delegate lifecycle-warning cases passed.")
