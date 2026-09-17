"""Payment-recording console behind the backend's every-other-day debt
nudge: the nudge message (see app/debt_nudge_job.py) carries one inline
button per debtor, and these handlers turn that message into the payment
it should produce — no need to open the dashboard.

Every case the operator can hit:
- the debt sits on a customer's OWN account  → credit attributed to that
  account (account_id + customer_id, same bucketing the web's per-account
  "Record payment" uses),
- on a group they REPRESENT                  → credit attributed to the
  group (group_id + their customer id as representative),
- directly on the customer (no account)      → credit with customer_id
  alone, "full" paying the customer's whole posted balance,
- full outstanding or ANY custom amount at every level,
- cancel at every step, a "customer already settled" guard (the debt may
  have been paid between the reminder and the tap), and backend errors
  surfaced as readable text instead of a silent dead button.

Conversation state is a plain in-process dict keyed by chat id — same
"popped, not read" reasoning as delegate_admin's _pending: this bot is
gated to one operator chat, and a pending payment is a few small fields
that live for seconds, not something that needs to survive a restart.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from api_client import backend
from handlers.common import admin_only, format_toman

logger = logging.getLogger(__name__)

# admin chat id -> conversation state (see module docstring)
_pending: dict[int, dict] = {}

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def _parse_amount(raw: str, outstanding: float) -> tuple[float | None, str | None]:
    """Returns (amount, error). Accepts Latin or Persian digits with optional
    thousand separators, or the word کامل/full meaning "the whole outstanding
    balance" — the common case by far."""
    text = (raw or "").strip().translate(_PERSIAN_DIGITS).replace(",", "")
    if text.lower() in ("کامل", "full", "all"):
        return outstanding, None
    try:
        amount = float(text)
    except ValueError:
        return None, "مبلغ باید یک عدد باشد، یا بنویسید «کامل»."
    if amount <= 0:
        return None, "مبلغ باید بزرگ‌تر از صفر باشد."
    return round(amount, 2), None


def _bucket_route(state: dict) -> dict:
    """Ledger attribution for this bucket — exactly the same one-owner-per-
    entry bucketing the web's Record payment uses (see services.MoneyBook)."""
    payload: dict = {"customer_id": state["customer_id"]}
    if state["kind"] == "a":
        payload["account_id"] = state["bucket_id"]
    elif state["kind"] == "g":
        payload["group_id"] = state["bucket_id"]
    return payload


@admin_only
async def debt_nudge_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Button on the backend's nudge message: opens this debtor's breakdown
    and the payment-entry keyboard."""
    query = update.callback_query
    await query.answer()
    try:
        customer_id = int(query.data.split(":")[1])
    except (IndexError, ValueError):
        await query.edit_message_text("این دکمه خراب است — از پیام جدید یادآوری دوباره امتحان کنید.")
        return

    try:
        cust = await backend.get(f"/api/customers/{customer_id}")
        accounts = await backend.get(f"/api/customers/{customer_id}/accounts")
        all_groups = await backend.get("/api/groups")
    except Exception as exc:  # noqa: BLE001
        await query.edit_message_text(f"خواندن اطلاعات مشتری شکست خورد: {exc}")
        return

    name = cust["name"]
    total = cust.get("balance", 0.0)
    if total <= 0:
        await query.edit_message_text(f"✅ {name} بدهی معوقه ندارد (احتمالاً بین یادآوری و الان تسویه شده).")
        return

    groups = [g for g in all_groups if g.get("representative_customer_id") == customer_id]

    lines = [f"💳 {name} — بدهی کل: {format_toman(total)}", "", "پرداخت برای کدام بخش ثبت شود؟"]
    keyboard: list[list[InlineKeyboardButton]] = []
    for a in accounts:
        if a.get("net_owed", 0) > 0:
            keyboard.append([InlineKeyboardButton(
                f"اکانت {a['marzban_username']} — {format_toman(a['net_owed'])}",
                callback_data=f"debtpay:a:{a['id']}:{customer_id}",
            )])
    for g in groups:
        keyboard.append([InlineKeyboardButton(
            f"گروه {g['name']}",
            callback_data=f"debtpay:g:{g['id']}:{customer_id}",
        )])
    keyboard.append([InlineKeyboardButton("بدون اکانت/گروه خاص", callback_data=f"debtpay:c:{customer_id}:{customer_id}")])
    keyboard.append([InlineKeyboardButton("بستن", callback_data="debtpay:x:0:0")])

    await query.edit_message_text(
        "\n".join(lines) + ("\n\n" + "\n".join(f"• {g['name']}" for g in groups) if groups else ""),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@admin_only
async def debt_pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A bucket was picked: read its outstanding balance and ask for the
    amount (پیش‌فرض: کامل)."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    try:
        _, kind, bucket_id, customer_id = query.data.split(":")
        bucket_id, customer_id = int(bucket_id), int(customer_id)
    except (IndexError, ValueError):
        await query.edit_message_text("این دکمه خراب است — از پیام جدید یادآوری دوباره امتحان کنید.")
        return
    if kind == "x":
        _pending.pop(chat_id, None)
        await query.edit_message_text("بسته شد.")
        return

    try:
        if kind == "a":
            account = await backend.get(f"/api/accounts/{bucket_id}")
            outstanding = (await backend.get(f"/api/ledger/balance?account_id={bucket_id}"))["balance"]
            desc = f"اکانت {account['marzban_username']}"
        elif kind == "g":
            group = await backend.get(f"/api/groups/{bucket_id}")
            outstanding = (await backend.get(f"/api/ledger/balance?group_id={bucket_id}"))["balance"]
            desc = f"گروه {group['name']}"
        else:
            outstanding = (await backend.get(f"/api/ledger/balance?customer_id={customer_id}"))["balance"]
            desc = "کل بدهی مشتری"
    except Exception as exc:  # noqa: BLE001
        await query.edit_message_text(f"خواندن موجودی شکست خورد: {exc}")
        return

    if outstanding <= 0:
        await query.edit_message_text(f"✅ بدهیِ این بخش تسویه شده (بین یادآوری و الان پرداخت شده).")
        return

    _pending[chat_id] = {
        "kind": kind,
        "bucket_id": bucket_id,
        "customer_id": customer_id,
        "outstanding": outstanding,
        "desc": desc,
    }
    await query.edit_message_text(
        f"ثبت پرداخت برای {desc}\n"
        f"بدهی این بخش: {format_toman(outstanding)}\n\n"
        "مبلغ را به تومان بفرستید، یا بنویسید «کامل».",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"کامل ({format_toman(outstanding)})", callback_data="debtdo:full"),
            InlineKeyboardButton("لغو", callback_data="debtdo:x"),
        ]]),
    )


@admin_only
async def debt_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Typed custom amount — must stay silent when no debt conversation is
    open for this chat, since text handlers see every message."""
    chat_id = update.effective_chat.id
    state = _pending.get(chat_id)
    if not state or not state.get("awaiting_amount"):
        return
    message = update.message
    if message is None:
        return

    amount, error = _parse_amount(message.text or "", state["outstanding"])
    if error:
        await message.reply_text(error)
        return
    state["amount"] = amount
    state["awaiting_amount"] = False
    await message.reply_text(
        f"ثبت پرداخت {format_toman(amount)} برای {state['desc']}؟",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ ثبت", callback_data="debtdo:confirm"),
            InlineKeyboardButton("❌ لغو", callback_data="debtdo:x"),
        ]]),
    )


@admin_only
async def debt_do_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Final confirm: post the credit through the backend's own ledger
    endpoint (same validation/attribution as the web's Record payment)."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    state = _pending.get(chat_id)
    _, action = query.data.split(":", 1)
    if action == "x" or state is None:
        _pending.pop(chat_id, None)
        await query.edit_message_text("لغو شد.")
        return

    if action == "full":
        amount = state["outstanding"]
    elif action == "confirm":
        amount = state.get("amount")
        if not amount:
            await query.edit_message_text("مبلغی انتخاب نشده — از نو شروع کنید.")
            return
    else:
        return

    payload = _bucket_route(state)
    payload.update({
        "type": "credit",
        "amount": amount,
        "note": f"Payment recorded via bot — {state['desc']}",
    })
    try:
        await backend.post("/api/ledger", json=payload)
    except Exception as exc:  # noqa: BLE001
        await query.edit_message_text(f"❌ ثبت پرداخت شکست خورد: {exc}")
        return

    try:
        customer_id = state["customer_id"]
        new_balance = (await backend.get(f"/api/ledger/balance?customer_id={customer_id}"))["balance"]
    except Exception:  # noqa: BLE001 — the payment DID post; never hide that behind a balance-read failure
        new_balance = None

    _pending.pop(chat_id, None)
    remaining = f"\nمانده بدهی مشتری: {format_toman(new_balance)}" if new_balance is not None else ""
    await query.edit_message_text(f"✅ پرداخت {format_toman(amount)} برای {state['desc']} ثبت شد.{remaining}")
