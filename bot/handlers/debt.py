"""Payment-recording console behind the backend's every-other-day debt
nudge: the nudge message carries one inline button per debtor, and these
handlers turn that message into a NAVIGABLE payment console — the same
message doubles as every screen, stepping list → debtor → amount →
confirm → back to the list, so nothing is ever a dead end:

- every screen ends with ↩️ back (to the previous screen) and ✖ بستن,
- after a payment posts, the console re-renders the debtor LIST itself
  with a success flash and live balances (via the backend's preview
  endpoint) — the operator lands back at the main menu, ready for the
  next debtor,
- every screen is derived from callback_data + fresh API reads, so
  buttons keep working even after a bot restart; the ONLY in-memory
  state is the typed-custom-amount bridge,
- the already-settled guard re-checks balances at every render (the
  debt may have been paid between the reminder and the tap), and the
  final ✅ ثبت confirm always stands between a tap and a posted credit.

Money posting goes through the backend's own /api/ledger endpoint — the
same validation/attribution the web's Record payment uses."""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from api_client import backend
from handlers.common import admin_only, format_toman

logger = logging.getLogger(__name__)

# The typed-custom-amount bridge: chat id -> {kind, bucket_id, customer_id,
# console_msg_id}. Deliberately the ONLY state — every button carries its
# full context in callback_data, so navigation survives restarts. Popped on
# every screen render that is not the amount screen.
_awaiting: dict[int, dict] = {}

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")

# Telegram caps an inline keyboard at 100 buttons; the hub shows at most 50
# debtors (two per row) and the backend's nudge keyboard obeys the same cap.
_MAX_LIST_BUTTONS = 50
_MAX_BUTTON_NAME = 14

Editor = Callable[[str, Optional[InlineKeyboardMarkup]], Awaitable[None]]


def _query_editor(query) -> Editor:
    async def edit(text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        await query.edit_message_text(text, reply_markup=markup)

    return edit


def _bot_editor(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> Editor:
    async def edit(text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)

    return edit


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def _nav_row(back_cb: Optional[str], back_label: str = "↩️ بازگشت") -> list[InlineKeyboardButton]:
    """The nav row every screen ends with — no dead ends anywhere."""
    row: list[InlineKeyboardButton] = []
    if back_cb:
        row.append(InlineKeyboardButton(back_label, callback_data=back_cb))
    row.append(InlineKeyboardButton("✖ بستن", callback_data="debtdo:close"))
    return [row]


def _remember_console(update: Update) -> None:
    """Keep the typed-amount bridge pointed at the message that is currently
    the console (the same message mutates through the screens)."""
    state = _awaiting.get(update.effective_chat.id)
    if state and update.callback_query is not None and update.callback_query.message is not None:
        state["console_msg_id"] = update.callback_query.message.message_id


def _stop_awaiting(chat_id: int) -> None:
    _awaiting.pop(chat_id, None)


def _truncate(name: str) -> str:
    return name if len(name) <= _MAX_BUTTON_NAME else name[:_MAX_BUTTON_NAME - 1] + "…"


def _parse_amount(raw: str) -> tuple[Optional[float], Optional[str]]:
    """(amount, error) for a typed figure — Latin or Persian digits with
    optional thousand separators. «کامل» is signalled as (None, None); the
    confirm screen resolves it against the live outstanding balance."""
    text = (raw or "").strip().translate(_PERSIAN_DIGITS).replace(",", "")
    if text.lower() in ("کامل", "full", "all"):
        return None, None
    try:
        amount = float(text)
    except ValueError:
        return None, "مبلغ باید یک عدد باشد، یا بنویسید «کامل»."
    if amount <= 0:
        return None, "مبلغ باید بزرگ‌تر از صفر باشد."
    return round(amount, 2), None


def _bucket_url(kind: str, bucket_id: int) -> str:
    if kind == "a":
        return f"/api/ledger/balance?account_id={bucket_id}"
    if kind == "g":
        return f"/api/ledger/balance?group_id={bucket_id}"
    return f"/api/ledger/balance?customer_id={bucket_id}"


def _bucket_payload(kind: str, bucket_id: int, customer_id: int) -> dict:
    """Ledger attribution for this bucket — exactly the one-owner-per-entry
    bucketing the web's Record payment uses (see services.MoneyBook)."""
    payload: dict = {"customer_id": customer_id}
    if kind == "a":
        payload["account_id"] = bucket_id
    elif kind == "g":
        payload["group_id"] = bucket_id
    return payload


async def _list_content(flash: str | None = None) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """(text, markup) for the hub list — shared by _render_list (which edits
    the console message) and the /debts command (which sends a fresh one)."""
    try:
        data = await backend.get("/api/notifications/debt-nudge")
        overdue: list[dict] = data["overdue"]
    except Exception as exc:  # noqa: BLE001
        return f"خواندن فهرست بدهی‌ها شکست خورد: {exc}", None

    lines: list[str] = []
    if flash:
        lines += [flash, ""]
    if not overdue:
        lines.append("✅ بدهی قدیمیِ بالای ۱۴ روز نیست — کاری نیست.")
        return "\n".join(lines), _kb(_nav_row(None))

    total = sum(r["amount"] for r in overdue)
    lines.append(f"⏳ بدهی‌های قدیمی — {len(overdue)} نفر، جمع {round(total):,} تومان")
    lines.append("برای ثبت پرداخت روی بدهکار بزنید (قدیمی‌ترین اول):")

    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for r in overdue[:_MAX_LIST_BUTTONS]:
        row.append(InlineKeyboardButton(
            f"{_truncate(r['name'])} · {round(r['amount']):,}",
            callback_data=f"debtnudge:{r['customer_id']}",
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔄 بروزرسانی", callback_data="debthub:refresh"),
                 InlineKeyboardButton("✖ بستن", callback_data="debtdo:close")])
    return "\n".join(lines), _kb(rows)


async def _render_list(edit: Editor, flash: str | None = None) -> None:
    """The hub screen: current overdue debtors, re-read live from the
    backend's preview endpoint so the list is never stale from message
    time."""
    text, markup = await _list_content(flash)
    await edit(text, markup)


@admin_only
async def debts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/debts — the payment console, on demand: same list the nudge carries,
    any time the operator wants it without waiting for the schedule."""
    text, markup = await _list_content()
    await update.message.reply_text(text, reply_markup=markup)


async def _render_debtor(edit: Editor, customer_id: int) -> None:
    try:
        cust = await backend.get(f"/api/customers/{customer_id}")
        accounts = await backend.get(f"/api/customers/{customer_id}/accounts")
        all_groups = await backend.get("/api/groups")
    except Exception as exc:  # noqa: BLE001
        await edit(f"خواندن اطلاعات مشتری شکست خورد: {exc}")
        return

    name = cust["name"]
    total = cust.get("balance", 0.0)
    if total <= 0:
        # Paid between the reminder and the tap — say so, land on the list.
        await _render_list(edit, flash=f"✅ {name} بدهی معوقه ندارد (احتمالاً همین حالا تسویه شده).")
        return

    groups = [g for g in all_groups if g.get("representative_customer_id") == customer_id]

    rows: list[list[InlineKeyboardButton]] = []
    for a in accounts:
        if a.get("net_owed", 0) > 0:
            rows.append([InlineKeyboardButton(
                f"اکانت {a['marzban_username']} — {format_toman(a['net_owed'])}",
                callback_data=f"debtpay:a:{a['id']}:{customer_id}",
            )])
    for g in groups:
        rows.append([InlineKeyboardButton(
            f"گروه {g['name']}",
            callback_data=f"debtpay:g:{g['id']}:{customer_id}",
        )])
    rows.append([InlineKeyboardButton(
        "بدون اکانت/گروه خاص",
        callback_data=f"debtpay:c:{customer_id}:{customer_id}",
    )])
    rows += _nav_row("debthub:refresh", "↩️ فهرست بدهی‌ها")
    await edit(
        f"💳 {name} — بدهی کل: {format_toman(total)}\n\nپرداخت برای کدام بخش ثبت شود؟",
        _kb(rows),
    )


async def _render_amount(edit: Editor, chat_id: int, kind: str, bucket_id: int, customer_id: int) -> None:
    try:
        if kind == "a":
            account = await backend.get(f"/api/accounts/{bucket_id}")
            desc = f"اکانت {account['marzban_username']}"
        elif kind == "g":
            group = await backend.get(f"/api/groups/{bucket_id}")
            desc = f"گروه {group['name']}"
        else:
            desc = "کل بدهی مشتری"
        outstanding = (await backend.get(_bucket_url(kind, bucket_id)))["balance"]
    except Exception as exc:  # noqa: BLE001
        await edit(f"خواندن موجودی شکست خورد: {exc}")
        return

    if outstanding <= 0:
        await _render_list(edit, flash="✅ بدهیِ این بخش تسویه شده (بین یادآوری و الان پرداخت شده).")
        return

    # Arm the typed-amount bridge for this chat — the ONLY state in the
    # module, and just enough to know where a typed number belongs.
    _awaiting[chat_id] = {
        "kind": kind,
        "bucket_id": bucket_id,
        "customer_id": customer_id,
        "desc": desc,
    }

    rows = [
        [InlineKeyboardButton(
            f"کامل ({format_toman(outstanding)})",
            callback_data=f"debtdo:confirm:{outstanding:g}:{kind}:{bucket_id}:{customer_id}",
        )],
        *_nav_row(f"debtnudge:{customer_id}"),
    ]
    await edit(
        f"🧾 ثبت پرداخت — {desc}\n"
        f"بدهی این بخش: {format_toman(outstanding)}\n\n"
        "مبلغ را به تومان بفرستید، یا «کامل» را بزنید:",
        _kb(rows),
    )


async def _render_confirm(edit: Editor, amount: Optional[float], kind: str, bucket_id: int,
                          customer_id: int) -> None:
    try:
        if kind == "a":
            account = await backend.get(f"/api/accounts/{bucket_id}")
            desc = f"اکانت {account['marzban_username']}"
        elif kind == "g":
            group = await backend.get(f"/api/groups/{bucket_id}")
            desc = f"گروه {group['name']}"
        else:
            desc = "کل بدهی مشتری"
        outstanding = (await backend.get(_bucket_url(kind, bucket_id)))["balance"]
    except Exception as exc:  # noqa: BLE001
        await edit(f"خواندن موجودی شکست خورد: {exc}")
        return

    if outstanding <= 0 and amount is None:
        await _render_list(edit, flash="✅ بدهیِ این بخش تسویه شده (بین یادآوری و الان پرداخت شده).")
        return

    resolved = amount if amount is not None else outstanding
    over = resolved > outstanding
    lines = [
        "🧾 تأیید نهایی ثبت پرداخت",
        f"{desc}",
        f"مبلغ: {format_toman(resolved)}",
        f"بدهی این بخش: {format_toman(outstanding)}",
    ]
    if over:
        lines.append("⚠️ مبلغ بیشتر از بدهی است — مازاد به‌عنوان اعتبار ثبت می‌شود.")

    rows = [
        [InlineKeyboardButton(
            f"✅ ثبت پرداخت {format_toman(resolved)}",
            callback_data=f"debtdo:post:{resolved:g}:{kind}:{bucket_id}:{customer_id}",
        )],
        *_nav_row(f"debtpay:{kind}:{bucket_id}:{customer_id}"),
    ]
    await edit("\n".join(lines), _kb(rows))


async def _post_and_back_to_list(edit: Editor, amount: float, kind: str, bucket_id: int,
                                 customer_id: int) -> None:
    try:
        if kind == "a":
            account = await backend.get(f"/api/accounts/{bucket_id}")
            desc = f"اکانت {account['marzban_username']}"
        elif kind == "g":
            group = await backend.get(f"/api/groups/{bucket_id}")
            desc = f"گروه {group['name']}"
        else:
            desc = "کل بدهی مشتری"
    except Exception:  # noqa: BLE001 — desc is cosmetic; never block the post on it
        desc = "بدهکار"

    payload = _bucket_payload(kind, bucket_id, customer_id)
    payload.update({
        "type": "credit",
        "amount": amount,
        "note": f"Payment recorded via bot — {desc}",
    })
    try:
        await backend.post("/api/ledger", json=payload)
    except Exception as exc:  # noqa: BLE001
        await edit(f"❌ ثبت پرداخت شکست خورد: {exc}")
        return

    try:
        new_balance = (await backend.get(f"/api/ledger/balance?customer_id={customer_id}"))["balance"]
    except Exception:  # noqa: BLE001 — the payment DID post; never hide that behind a balance-read failure
        new_balance = None

    flash = f"✅ پرداخت {format_toman(amount)} برای {desc} ثبت شد."
    if new_balance is not None:
        flash += f" مانده مشتری: {format_toman(new_balance)}"
    # Back to the main menu — the list, with the payment reflected in the
    # fresh balances the preview endpoint returns.
    await _render_list(edit, flash=flash)


@admin_only
async def debt_nudge_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Button on the nudge message: render this debtor's bucket breakdown."""
    query = update.callback_query
    await query.answer()
    _stop_awaiting(update.effective_chat.id)
    _remember_console(update)
    try:
        customer_id = int(query.data.split(":")[1])
    except (IndexError, ValueError):
        await query.edit_message_text("این دکمه خراب است — از پیام جدید یادآوری دوباره امتحان کنید.")
        return
    await _render_debtor(_query_editor(query), customer_id)


@admin_only
async def debt_hub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List screen's own buttons: 🔄 بروزرسانی re-reads the live list;
    ✖ بستن closes the console."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    _stop_awaiting(chat_id)
    action = query.data.split(":")[1] if ":" in query.data else ""
    if action == "close":
        await query.edit_message_text("بسته شد.")
        return
    await _render_list(_query_editor(query))


@admin_only
async def debt_pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A bucket was picked: render the amount screen (کامل or typed)."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    _stop_awaiting(chat_id)
    try:
        _, kind, bucket_id, customer_id = query.data.split(":")
        bucket_id, customer_id = int(bucket_id), int(customer_id)
        if kind not in ("a", "g", "c"):
            raise ValueError(kind)
    except (IndexError, ValueError):
        await query.edit_message_text("این دکمه خراب است — از پیام جدید یادآوری دوباره امتحان کنید.")
        return
    await _render_amount(_query_editor(query), chat_id, kind, bucket_id, customer_id)
    # The amount screen arms the typed-amount bridge; point it at THIS
    # message so a typed figure edits the console, not a stray reply.
    _remember_console(update)


@admin_only
async def debt_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Typed custom amount — must stay silent when no debt conversation is
    open for this chat, since text handlers see every message. The console
    message is edited into the confirm screen (never a stray reply), and the
    typed message itself is deleted to keep the chat clean."""
    chat_id = update.effective_chat.id
    state = _awaiting.get(chat_id)
    if not state:
        return
    message = update.message
    if message is None:
        return

    amount, error = _parse_amount(message.text or "")
    if error:
        await message.reply_text(error)
        return

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    except Exception:  # noqa: BLE001 — cosmetic; the flow continues either way
        pass

    _stop_awaiting(chat_id)
    editor = _bot_editor(context, chat_id, state.get("console_msg_id") or message.message_id)
    await _render_confirm(editor, amount, state["kind"], state["bucket_id"], state["customer_id"])


@admin_only
async def debt_do_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """debtdo: router — confirm screen, the actual post, and close. Every
    action re-derives its context from callback_data, so old buttons and
    post-restart buttons behave the same."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    _remember_console(update)
    parts = query.data.split(":")

    if len(parts) < 2:
        return
    action = parts[1]

    if action == "close" or (action == "x" and len(parts) == 2):
        _stop_awaiting(chat_id)
        await query.edit_message_text("بسته شد.")
        return

    if action == "confirm" and len(parts) == 6:
        _stop_awaiting(chat_id)
        try:
            amount = float(parts[2])
            kind, bucket_id, customer_id = parts[3], int(parts[4]), int(parts[5])
        except ValueError:
            await query.edit_message_text("این دکمه خراب است — از پیام جدید یادآوری دوباره امتحان کنید.")
            return
        await _render_confirm(_query_editor(query), amount, kind, bucket_id, customer_id)
        return

    if action == "post" and len(parts) == 6:
        _stop_awaiting(chat_id)
        try:
            amount = float(parts[2])
            kind, bucket_id, customer_id = parts[3], int(parts[4]), int(parts[5])
        except ValueError:
            await query.edit_message_text("این دکمه خراب است — از پیام جدید یادآوری دوباره امتحان کنید.")
            return
        await _post_and_back_to_list(_query_editor(query), amount, kind, bucket_id, customer_id)
        return

    # Old-format buttons from before the console existed (debtdo:full /
    # debtdo:x without a bucket) can no longer act — say so instead of
    # pretending.
    if action == "x":
        _stop_awaiting(chat_id)
        await query.edit_message_text("لغو شد.")
        return
    await query.edit_message_text("این دکمه مال نسخه‌ی قبلی است — از پیام جدید یادآوری دوباره شروع کنید.")
