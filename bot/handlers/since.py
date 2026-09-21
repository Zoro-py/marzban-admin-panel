"""«از این تاریخ» — the /since command, part of the operator's Telegram
toolbox (R22): the Balance-since widget from the dashboard, in the chat.

/since <نام یا id> [تاریخ] — resolves a customer (id or name), an account
(username) or a group (numeric id), then reads GET /api/ledger/balance with
the since boundary and renders the four rows the operator actually asks
about: charged GB with its Toman, consumed GB with its Toman, payments
received, and the still-uninvoiced accrual — plus the running debt FROM
that date.

Date forms accepted: Jalali (1405/06/01), Gregorian (2026-08-22), or a
relative 30d. Omitted → the first day of the CURRENT Jalali month — the
"این ماه" reading most operators mean. Persian digits are accepted — an
Iranian keyboard auto-substitutes them and rejecting them here would make
the command look broken.

Read-only: like /bill, this never posts anything.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import jdatetime
from telegram import Update
from telegram.ext import ContextTypes

from api_client import backend
from handlers.common import AmbiguousMatch, admin_only, format_toman, resolve_account, resolve_customer

logger = logging.getLogger(__name__)

_TEHRAN = timedelta(hours=3, minutes=30)

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")

USAGE = (
    "Usage: /since <نام یا id> [1405/06/01 | 2026-08-22 | 30d]\n"
    "مثال: /since boojar_family 1405/06/01\n"
    "بدون تاریخ = اولِ ماه جلالی جاری."
)


def _normalize_digits(text: str) -> str:
    return (text or "").translate(_PERSIAN_DIGITS).strip()


def parse_since(raw: Optional[str], today_utc: Optional[date] = None) -> str:
    """Date argument → an ISO 'YYYY-MM-DD' string, the form
    GET /api/ledger/balance accepts. Jalali vs Gregorian is detected by the
    first component: >= 1700 means a Gregorian year (Jalali years are 13xx-14xx), otherwise a Jalali one.
    Raises ValueError with operator-readable text on garbage."""
    today = today_utc or datetime.now(timezone.utc).date()
    if raw is None or raw == "":
        today_j = jdatetime.date.fromgregorian(date=today)
        return jdatetime.date(today_j.year, today_j.month, 1).togregorian().isoformat()

    text = _normalize_digits(raw).lower()

    if text.endswith("d"):
        try:
            days = int(text[:-1])
        except ValueError:
            raise ValueError(f"'{raw}' — عدد روزها درست نیست. {USAGE}")
        if days <= 0:
            raise ValueError(f"'{raw}' — روزها باید مثبت باشند. {USAGE}")
        return (today - timedelta(days=days)).isoformat()

    for sep in ("/", "-"):
        if sep in text:
            parts = text.split(sep)
            if len(parts) == 3:
                try:
                    y, m, d = (int(p) for p in parts)
                except ValueError:
                    break
                # Jalali years run ~1300-1500, Gregorian ones 1900+ — the
                # 1000 cut I first wrote swallowed 14xx as a Gregorian year
                # and handed back the un-converted string.
                if y >= 1700:  # 2026-08-22
                    return date(y, m, d).isoformat()
                # 1405/06/01 — jdatetime validates month/day itself
                return jdatetime.date(y, m, d).togregorian().isoformat()
    raise ValueError(f"'{raw}' را نفهمیدم. {USAGE}")


async def _resolve_scope(query: str) -> tuple[str, int, str]:
    """query → (kind, id, display). Customer wins ties (a family's customer
    and its first account often share a name; the customer is the thing the
    operator bills)."""
    customer = await resolve_customer(query)
    if customer is not None:
        return "customer", customer["id"], f"{customer['name']} (مشتری #{customer['id']})"
    account = await resolve_account(query)
    if account is not None:
        return "account", account["id"], f"{account['marzban_username']} (اکانت #{account['id']})"
    if query.isdigit():
        group = await backend.get(f"/api/groups/{query}")
        if group is not None:
            return "group", group["id"], f"{group['name']} (گروه #{group['id']})"
    return None, None, None  # type: ignore[return-value]


def _fmt_gb(v) -> str:
    return "—" if v is None else f"{v:g} GB"


async def since_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = [a for a in (context.args or []) if a.strip()]
    if not args:
        await update.message.reply_text(USAGE)
        return

    query = args[0]
    try:
        since_iso = parse_since(args[1] if len(args) > 1 else None)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    try:
        kind, entity_id, display = await _resolve_scope(query)
    except AmbiguousMatch as exc:
        names = "، ".join(m["name"] for m in exc.matches[:6])
        await update.message.reply_text(f"«{query}» مبهم است — کدام یکی؟ {names}")
        return
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"خواندن از پنل شکست خورد: {exc}")
        return

    if kind is None:
        await update.message.reply_text(
            f"هیچ مشتری/اکانت/گروهی به نام «{query}» پیدا نشد. "
            f"برای گروه، id عددی بدهید. {USAGE}")
        return

    params = {f"{kind}_id": entity_id, "since": since_iso}
    try:
        bal = await backend.get("/api/ledger/balance", params=params)
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"خواندن balance شکست خورد: {exc}")
        return

    # The boundary the backend actually used — worth showing verbatim, since
    # Jalali input and the UTC boundary the SQL applies are different dates
    # on the wall clock and the operator is the one reconciling receipts.
    try:
        since_local = datetime.fromisoformat(since_iso) + _TEHRAN
        boundary = since_local.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        boundary = since_iso

    posted = bal.get("balance") or 0.0
    pending = bal.get("pending_amount") or 0.0
    net = bal.get("net_owed")
    if net is None:  # older backend without the headline field
        net = posted + pending
    n_all, n_gb = bal.get("charge_count"), bal.get("charge_count_with_gb")
    if n_all is not None:
        charged = f"شارژها: {n_all} مورد — {format_toman(bal.get('charged_amount') or 0.0)}"
    else:
        charged = f"شارژشده: {format_toman(bal.get('charged_amount') or 0.0)}"
    if bal.get("gb_charged") is not None:
        partial = n_all is not None and n_gb is not None and n_gb < n_all
        charged += f" (حجم ثبت‌شده: {_fmt_gb(bal.get('gb_charged'))}" + (f" فقط روی {n_gb} از {n_all}" if partial else "") + ")"
    elif n_all:
        charged += " (حجم ثبت نشده)"
    lines = [
        f"📅 از {since_iso} (بامداد UTC → {boundary} به وقت تهران)",
        f"— {display}",
        charged,
        f"مصرف صورت‌حساب‌شده: {_fmt_gb(bal.get('gb_consumed'))} — {format_toman(bal.get('consumed_amount') or 0.0)}",
        f"پرداخت‌شده: {format_toman(bal.get('credited_amount') or 0.0)}",
        f"صورتحساب‌شده از این تاریخ: {format_toman(posted)}",
    ]
    if pending > 0:
        gb = bal.get("pending_gb")
        lines.append(f"هنوز صورتحساب‌نشده: {format_toman(pending)}" + (f" ({_fmt_gb(gb)})" if gb else ""))
    lines.append(f"بدهی از این تاریخ (با صورتحساب‌نشده‌ها): {format_toman(net)}")
    await update.message.reply_text("\n".join(lines))
