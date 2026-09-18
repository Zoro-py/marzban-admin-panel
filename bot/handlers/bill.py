"""صورتحساب — the /bill command, part of the operator's Telegram toolbox
(the typing-bar command menu lists it): one customer's whole money picture
in a single message — posted balance, not-yet-invoiced usage WITH its Toman
equivalent, and the latest transactions — plus a 🔄 بروزرسانی button that
re-reads everything live and a 💳 ثبت پرداخت button that hands the message
over to the debt console (handlers/debt.py) to actually record a payment.

Read-only: /bill never posts anything; the payment path is the console's,
behind its own confirm step."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from api_client import backend
from handlers.common import AmbiguousMatch, admin_only, format_toman, resolve_account, resolve_customer

logger = logging.getLogger(__name__)

# Iran is UTC+3:30 year-round (DST abolished 2022) — ledger dates are naive
# UTC, and the operator reads Tehran wall-clock.
_TEHRAN = timedelta(hours=3, minutes=30)

_MAX_TX_ROWS = 8
_MAX_BUTTON_NAME = 14

Editor = Callable[[str, Optional[InlineKeyboardMarkup]], Awaitable[None]]


def _query_editor(query) -> Editor:
    async def edit(text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        await query.edit_message_text(text, reply_markup=markup)

    return edit


def _reply_editor(update: Update) -> Editor:
    """For the command itself: there is no message to edit yet, so the
    first render is a fresh reply — the buttons on it then drive the edits."""
    async def edit(text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        await update.message.reply_text(text, reply_markup=markup)

    return edit


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def _truncate(name: str) -> str:
    return name if len(name) <= _MAX_BUTTON_NAME else name[:_MAX_BUTTON_NAME - 1] + "…"


def _tehran_stamp(date_str: str) -> str:
    try:
        dt = datetime.fromisoformat(date_str.replace(" ", "T")) + _TEHRAN
        return dt.strftime("%m/%d %H:%M")
    except (ValueError, TypeError):
        return (date_str or "")[:10]


def _bill_rows(customer_id: int) -> list[list[InlineKeyboardButton]]:
    return [
        [InlineKeyboardButton("🔄 بروزرسانی", callback_data=f"bill:refresh:{customer_id}"),
         InlineKeyboardButton("💳 ثبت پرداخت", callback_data=f"debtnudge:{customer_id}")],
        [InlineKeyboardButton("✖ بستن", callback_data="bill:close")],
    ]


async def _render_bill(edit: Editor, customer: dict) -> None:
    """One live read of everything the card needs: posted balance, the
    window-blind accruing usage with its Toman sibling (the same figures the
    account card shows), and the latest ledger rows with account names."""
    cid = customer["id"]
    try:
        balance = await backend.get("/api/ledger/balance", params={"customer_id": cid})
        accounts = await backend.get(f"/api/customers/{cid}/accounts")
        txs = await backend.get("/api/ledger", params={"customer_id": cid, "limit": _MAX_TX_ROWS})
    except Exception as exc:  # noqa: BLE001
        await edit(f"خواندن صورتحساب شکست خورد: {exc}")
        return

    posted = balance.get("balance", 0.0)
    pending_amount = balance.get("pending_amount")
    gb_pending = balance.get("gb_pending")
    username_by_id = {a["id"]: a["marzban_username"] for a in accounts}

    owe = "بدهکار" if posted > 0 else ("اعتبار" if posted < 0 else "تسویه")
    lines = [f"🧾 صورتحساب {customer['name']} (#{cid})",
             f"بدهی ثبت‌شده: {format_toman(posted)} — {owe}"]
    if gb_pending:
        toman = format_toman(pending_amount) if pending_amount is not None else "—"
        lines.append(f"هنوز صورت‌حساب نشده: {gb_pending:g} GB ({toman})")
        lines.append(f"اگر همین حالا تسویه شود: {format_toman(posted + (pending_amount or 0.0))}")

    if txs:
        lines.append("")
        lines.append("تراکنش‌های اخیر:")
        for t in txs:
            label = "بدهی" if t.get("type") == "charge" else "پرداخت"
            sign = "+" if t.get("type") == "charge" else "−"
            where = username_by_id.get(t.get("account_id"))
            where = f" · {where}" if where else ""
            lines.append(f"• {_tehran_stamp(t.get('date') or '')} — {label} {sign}{round(t.get('amount') or 0):,}{where}")
    else:
        lines.append("تراکنشی ثبت نشده.")

    await edit("\n".join(lines), _kb(_bill_rows(cid)))


@admin_only
async def bill_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/bill — with a name/id renders that customer's bill; with no argument
    offers the current debtors as one-tap picks."""
    if not context.args:
        try:
            overdue = (await backend.get("/api/notifications/debt-nudge"))["overdue"]
        except Exception as exc:  # noqa: BLE001
            await update.message.reply_text(f"خواندن فهرست بدهی‌ها شکست خورد: {exc}")
            return
        rows: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []
        for r in overdue[:50]:
            row.append(InlineKeyboardButton(
                f"{_truncate(r['name'])} · {round(r['amount']):,}",
                callback_data=f"bill:show:{r['customer_id']}",
            ))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("✖ بستن", callback_data="bill:close")])
        await update.message.reply_text(
            "صورتحساب کدام مشتری؟ (یا بنویسید: /bill <نام یا id>)",
            reply_markup=_kb(rows),
        )
        return

    query = " ".join(context.args)
    try:
        customer = await resolve_customer(query)
    except AmbiguousMatch as exc:
        rows = [[InlineKeyboardButton(
            f"#{c['id']} — {_truncate(c['name'])}",
            callback_data=f"bill:show:{c['id']}",
        )] for c in exc.matches[:12]]
        rows.append([InlineKeyboardButton("✖ بستن", callback_data="bill:close")])
        await update.message.reply_text("چند مشتری matched شدند — یکی را انتخاب کن:", reply_markup=_kb(rows))
        return

    if customer is None:
        # Not a customer name/id — maybe a Marzban account username.
        account = await resolve_account(query)
        if account is None or not account.get("customer_id"):
            await update.message.reply_text(f"No customer matches '{query}'.")
            return
        customer = await backend.get(f"/api/customers/{account['customer_id']}")

    await _render_bill(_reply_editor(update), customer)


@admin_only
async def bill_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """bill:refresh:{id} re-reads the bill live; bill:show:{id} opens one
    from the quick-pick list; bill:close closes."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    if parts[1] == "close":
        await query.edit_message_text("بسته شد.")
        return
    if len(parts) != 3 or parts[1] not in ("refresh", "show"):
        return
    try:
        customer_id = int(parts[2])
    except ValueError:
        await query.edit_message_text("این دکمه خراب است — دوباره /bill را بزنید.")
        return
    try:
        customer = await backend.get(f"/api/customers/{customer_id}")
    except Exception as exc:  # noqa: BLE001
        await query.edit_message_text(f"خواندن اطلاعات مشتری شکست خورد: {exc}")
        return
    await _render_bill(_query_editor(query), customer)
