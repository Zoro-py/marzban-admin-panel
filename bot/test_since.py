"""Behavioral test for the /since command (R22): date parsing (Jalali,
Gregorian, relative, Persian digits, default = first of the current Jalali
month), entity resolution (customer > account > group), ambiguous names,
and the rendered rows. Backend is mocked — read-only, nothing posted.

Run from bot/:  venv/Scripts/python test_since.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("ADMIN_CHAT_ID", "777")
os.environ.setdefault("API_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jdatetime  # noqa: E402

import handlers.common as common  # noqa: E402
import handlers.since as since  # noqa: E402

failures: list[str] = []
ADMIN = 777


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class FakeBackend:
    async def get(self, url: str, params=None):
        if url == "/api/customers":
            return [{"id": 13, "name": "boojar_family"},
                    {"id": 5, "name": "Ali"}, {"id": 6, "name": "Alireza"}]
        if url == "/api/accounts":
            return [{"id": 54, "marzban_username": "Sadegi", "customer_id": 50}]
        if url == "/api/groups/2":
            return {"id": 2, "name": "company-x"}
        if url.startswith("/api/ledger/balance"):
            return {
                "entity_type": params.get("customer_id") and "customer" or "account",
                "balance": 775000.0, "gb_charged": 155.0, "gb_consumed": None,
                "consumed_amount": None, "charged_amount": 1025000.0,
                "credited_amount": 250000.0, "gb_pending": 28.961,
                "pending_amount": 500000.0, "net_owed": 1275000.0, "pending_gb": 100.0,
                "charge_count": 9, "charge_count_with_gb": 3, "charged_amount_gb_known": 400000.0,
            }
        raise AssertionError(f"unexpected GET {url} {params}")


class FakeMessage:
    def __init__(self):
        self.texts: list[str] = []

    async def reply_text(self, text, **_):
        self.texts.append(text)


async def run(args: list[str]) -> FakeMessage:
    since.backend = FakeBackend()
    common.backend = since.backend
    msg = FakeMessage()
    ctx = SimpleNamespace(args=args)
    await since.since_command(SimpleNamespace(message=msg), ctx)
    return msg


def main() -> None:
    today = date(2026, 9, 20)

    print("== date parsing ==")
    check("Jalali 1405/06/01 → 2026-08-23",
          since.parse_since("1405/06/01", today) == "2026-08-23",
          since.parse_since("1405/06/01", today))
    check("Jalali dashes accepted",
          since.parse_since("1405-06-01", today) == "2026-08-23")
    check("Gregorian 2026-08-22 kept",
          since.parse_since("2026-08-22", today) == "2026-08-22")
    check("Persian digits converted",
          since.parse_since("۱۴۰۵/۰۶/۰۱", today) == "2026-08-23")
    check("relative 30d", since.parse_since("30d", today) == "2026-08-21")
    first_j_month = jdatetime.date(1405, 6, 1).togregorian().isoformat()
    check("no date → first of current Jalali month (1405-06 → 2026-08-23)",
          since.parse_since(None, today) == first_j_month, since.parse_since(None, today))
    for bad in ("tomorrow", "1405/13/01", "0d", "-5d"):
        try:
            since.parse_since(bad, today)
            check(f"garbage '{bad}' rejected", False)
        except ValueError:
            check(f"garbage '{bad}' rejected", True)

    print("== resolution + render ==")
    msg = asyncio.run(run(["boojar_family", "1405/06/01"]))
    text = "\n".join(msg.texts)
    check("customer resolved and labeled", "boojar_family (مشتری #13)" in text, text[:80])
    check("charge rows present (count + GB coverage), usage, payments, not-invoiced",
          all(k in text for k in ("شارژها: 9 مورد", "فقط روی 3 از 9", "مصرف صورت‌حساب‌شده", "پرداخت‌شده", "هنوز صورتحساب‌نشده")))
    check("headline is the NET incl. not-invoiced (1,275,000), posted shown separately",
          "1,275,000 T" in text and "775,000 T" in text and "بدهی از این تاریخ (با صورتحساب‌نشده‌ها): 1,275,000 T" in text, text)
    check("not-invoiced line carries the billable GB (100 GB), not live usage", "500,000 T (100 GB)" in text, text)
    check("None GB renders as —", "—" in text)
    check("debt line present", "بدهی از این تاریخ" in text)

    msg2 = asyncio.run(run(["Sadegi"]))
    check("account username resolves", any("Sadegi (اکانت #54)" in t for t in msg2.texts))

    msg3 = asyncio.run(run(["2"]))
    check("numeric id that is no customer/account falls to group",
          any("company-x (گروه #2)" in t for t in msg3.texts), str(msg3.texts))

    msg4 = asyncio.run(run(["Al"]))
    check("ambiguous prefix refused with candidates",
          any("مبهم" in t and "Alireza" in t for t in msg4.texts), str(msg4.texts))

    msg5 = asyncio.run(run(["nobody-here"]))
    check("unknown name refused", any("پیدا نشد" in t for t in msg5.texts))

    msg6 = asyncio.run(run([]))
    check("no args → usage", any("Usage:" in t for t in msg6.texts))

    print()
    if failures:
        print(f"RESULT: {len(failures)} FAILURES: {failures}")
        sys.exit(1)
    print("RESULT: all checks OK")


main()
