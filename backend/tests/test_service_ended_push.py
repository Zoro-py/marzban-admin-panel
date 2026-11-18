"""The END-of-service push (2026-11-18, owner GO): the reminder chain had a
hole at its end. The expiry warning fires 3 days before death and the usage
warning at 80% — both deliberately bounded ABOVE the end (the sweep's own
conditions are `0 < seconds_left` and `percent < 100`) — so the moment the
service actually died, nothing fired: the customer discovered the outage by
their VPN failing, and had to open the bot to learn why.

  * volume exhausted (>= 100%) → ONE «قطع شد — حجمش تمام شد» push with the
    renew button on it (same per-account target as the warnings);
  * time up (expire <= now) → «قطع شد — زمانش تمام شد»;
  * trial expired → the trial's death push with the buy button;
  * each fires ONCE per end-event (renewals mint a NEW order on the same
    account and only the newest order speaks, so the mark resets naturally);
  * a healthy account still says nothing.

Schema: one nullable column (shoporder.ended_warned_at), added by db.py's
own startup migration pattern — additive, no operator step.

Plain `python -m tests.test_service_ended_push` from `backend/`.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="ended_push_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""
os.environ["SHOP_BOT_API_KEY"] = ""
os.environ["DELEGATE_BOT_API_KEY"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    ShopOrder,
    ShopOrderStatus,
    ShopUser,
    utcnow,
)
from datetime import timedelta  # noqa: E402

from app import shop_texts  # noqa: E402
from app.shop_service import warn_customers_before_service_ends  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


_SENT: list[tuple] = []


async def _capture(chat_id, text, reply_markup=None):
    _SENT.append((chat_id, text, reply_markup))


def _mk_order(session: Session, *, tg: int, username: str, used: int, limit: int,
              expire: int | None, price: int = 100_000, nick: str | None = None) -> ShopOrder:
    """A delivered paid (or trial, price=0) order on its own account, whose
    delivery is older than the 24h warning grace."""
    user = ShopUser(telegram_id=tg, display_name=f"u{tg}")
    session.add(user)
    session.commit()
    session.refresh(user)
    account = Account(marzban_username=username, used_traffic=used, data_limit=limit,
                      expire=expire, status="active", service_name=nick)
    session.add(account)
    session.commit()
    session.refresh(account)
    order = ShopOrder(shop_user_id=user.id, data_limit_gb=limit / (1024 ** 3),
                      duration_days=30, price=price,
                      status=ShopOrderStatus.delivered, account_id=account.id,
                      marzban_username=username)
    session.add(order)
    session.commit()
    session.refresh(order)
    order.delivered_at = utcnow() - timedelta(days=8)
    session.add(order)
    session.commit()
    session.refresh(order)
    return order


async def _sweep() -> None:
    with Session(engine) as session:
        await warn_customers_before_service_ends(session)


GB = 1024 ** 3
NOW = int(time.time())


def _texts_exist() -> None:
    check("shop_texts.service_ended exists",
          callable(getattr(shop_texts, "service_ended", None)))
    check("shop_texts.trial_ended exists",
          callable(getattr(shop_texts, "trial_ended", None)))


async def main() -> None:
    from app import notify as notify_module
    original = notify_module.send_to_shop_user
    notify_module.send_to_shop_user = _capture
    try:
        _texts_exist()

        # ── 1. volume exhausted → the death push, once ────────────────────
        with Session(engine) as session:
            order = _mk_order(session, tg=771, username="ended-vol", used=10 * GB,
                              limit=10 * GB, expire=NOW + 20 * 86400)
            oid = order.id
        _SENT.clear()
        await _sweep()
        hit = next((s for s in _SENT if s[0] == 771), None)
        check("volume death → exactly ONE push", len([s for s in _SENT if s[0] == 771]) == 1,
              repr([s[1] for s in _SENT]))
        check("the push names the death reason (حجمش تمام شد)",
              hit is not None and "قطع شد" in hit[1] and "حجمش تمام شد" in hit[1],
              repr(hit[1] if hit else None))
        check("the push carries a per-account renew button",
              hit is not None and hit[2] is not None
              and any(b.get("callback_data", "").startswith("ren:")
                      for row_ in hit[2].get("inline_keyboard", []) for b in row_),
              repr(hit[2] if hit else None))
        with Session(engine) as session:
            o = session.get(ShopOrder, oid)
            check("the order is marked once (ended_warned_at set)",
                  getattr(o, "ended_warned_at", "MISSING") not in (None, "MISSING"),
                  repr(getattr(o, "ended_warned_at", "MISSING")))
        await _sweep()
        check("the death push never repeats",
              len([s for s in _SENT if s[0] == 771]) == 1, repr([s[1] for s in _SENT]))

        # ── 2. time up → the death push with the time reason ──────────────
        with Session(engine) as session:
            order = _mk_order(session, tg=772, username="ended-time", used=1 * GB,
                              limit=10 * GB, expire=NOW - 60)
            oid = order.id
        _SENT.clear()
        await _sweep()
        hit = next((s for s in _SENT if s[0] == 772), None)
        check("time death → ONE push naming زمانش تمام شد",
              hit is not None and "زمانش تمام شد" in hit[1], repr(hit[1] if hit else None))
        with Session(engine) as session:
            check("...and it is marked",
                  getattr(session.get(ShopOrder, oid), "ended_warned_at", None) is not None)

        # ── 3. trial expired → the trial's death push with the buy button ──
        with Session(engine) as session:
            _mk_order(session, tg=773, username="ended-trial", used=0,
                      limit=1 * GB, expire=NOW - 120, price=0)
        _SENT.clear()
        await _sweep()
        hit = next((s for s in _SENT if s[0] == 773), None)
        check("trial death → ONE «تست رایگانتان تمام شد» push",
              hit is not None and "تست رایگانتان تمام شد" in hit[1],
              repr(hit[1] if hit else None))
        check("trial death carries the buy button (renew:<gb>:<ts>)",
              hit is not None and hit[2] is not None
              and any(b.get("callback_data", "").startswith("renew:")
                      for row_ in hit[2].get("inline_keyboard", []) for b in row_),
              repr(hit[2] if hit else None))

        # ── 4. WHICH service? (owner report 2026-11-18: «قابل فهم نیست کدوم
        #      اکانت تموم شده») ─────────────────────────────────────────────
        # a named service → the push LEADS with the customer's own nickname
        with Session(engine) as session:
            _mk_order(session, tg=775, username="ended-nick", used=30 * GB,
                      limit=30 * GB, expire=NOW + 20 * 86400, nick="گوشی علی")
        _SENT.clear()
        await _sweep()
        hit = next((s for s in _SENT if s[0] == 775), None)
        check("named service → the push leads with «گوشی علی»",
              hit is not None and "«گوشی علی»" in hit[1] and "قطع شد" in hit[1],
              repr(hit[1] if hit else None))
        check("named push still names the death reason",
              hit is not None and "حجمش تمام شد" in hit[1], repr(hit[1] if hit else None))

        # an UNNAMED service (multi-service customer, same sizes) → a pointer
        # to «سرویس‌های من», because the size alone cannot disambiguate
        with Session(engine) as session:
            _mk_order(session, tg=776, username="ended-unnamed", used=30 * GB,
                      limit=30 * GB, expire=NOW + 20 * 86400)
        _SENT.clear()
        await _sweep()
        hit = next((s for s in _SENT if s[0] == 776), None)
        check("unnamed service → the push points to «سرویس‌های من»",
              hit is not None and "سرویس‌های من" in hit[1], repr(hit[1] if hit else None))
        check("unnamed push keeps the button-reassures line",
              hit is not None and "همین سرویس" in hit[1], repr(hit[1] if hit else None))

        # ── 5. a healthy account still says nothing ────────────────────────
        with Session(engine) as session:
            _mk_order(session, tg=774, username="healthy", used=1 * GB,
                      limit=10 * GB, expire=NOW + 20 * 86400)
        _SENT.clear()
        await _sweep()
        check("healthy account → silence", len(_SENT) == 0, repr([s[1] for s in _SENT]))
    finally:
        notify_module.send_to_shop_user = original


asyncio.run(main())

print()
if failures:
    print(f"RESULT: {len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("RESULT: service-ended push checks OK")
