"""The delegate self-service conversation.

Stateless by design: every callback_data string carries everything the next
step needs (account_id, gb) directly, rather than stashing it in
context.user_data/chat_data between messages. bot/handlers/wallet.py needs
module-level pending state because ITS confirm step guards a money-moving
action against a double-tap; here a double-tap on "confirm delete" is
already handled by Marzban's own delete being idempotent (see
marzban_client.delete_user's 404-is-success comment), and create/renew are
each a single tap with no separate confirm step at all — see the design
note in backend/app/delegate_service.py's module docstring for why that's
safe here (fixed-preset volumes, scoped to the delegate's own accounts,
no stranger-targeting risk the way wallet.py's /wallet has).
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

import texts
from api_client import DelegateApiError, backend


async def _session(update: Update) -> dict | None:
    """None means "not a delegate" (backend returned 403) — every caller
    must handle that by showing texts.NOT_A_DELEGATE, never by assuming a
    session exists."""
    try:
        return await backend.post("/api/delegate/bot/session", json={"telegram_id": update.effective_user.id})
    except DelegateApiError as exc:
        if exc.status == 403:
            return None
        raise


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[texts.MENU_NEW, texts.MENU_LIST]], resize_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        session = await _session(update)
    except DelegateApiError:
        await update.effective_message.reply_text(texts.GENERIC_ERROR)
        return
    if session is None:
        await update.effective_message.reply_text(texts.NOT_A_DELEGATE)
        return
    await update.effective_message.reply_text(texts.welcome(session["scope_name"]), reply_markup=main_menu())


async def _offer_new_account(update: Update) -> None:
    try:
        session = await _session(update)
    except DelegateApiError:
        await update.effective_message.reply_text(texts.GENERIC_ERROR)
        return
    if session is None:
        await update.effective_message.reply_text(texts.NOT_A_DELEGATE)
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{g}GB", callback_data=f"d:new:{g}") for g in session["quick_volumes_gb"]
    ]])
    await update.effective_message.reply_text(texts.pick_volume_for_new(), reply_markup=keyboard)


async def _list_accounts(update: Update) -> None:
    try:
        session = await _session(update)
    except DelegateApiError:
        await update.effective_message.reply_text(texts.GENERIC_ERROR)
        return
    if session is None:
        await update.effective_message.reply_text(texts.NOT_A_DELEGATE)
        return
    try:
        accounts = await backend.get("/api/delegate/bot/accounts", params={"telegram_id": update.effective_user.id})
    except DelegateApiError:
        await update.effective_message.reply_text(texts.GENERIC_ERROR)
        return
    if not accounts:
        await update.effective_message.reply_text(texts.no_accounts_yet(), reply_markup=main_menu())
        return

    # Capped, not paginated — see the module note in
    # backend/app/routers/delegate.py's docstring for the accepted v1
    # limitation on very large account counts.
    shown = accounts[:25]
    for account in shown:
        line = texts.account_line(
            account["marzban_username"], account["data_limit"], account["expire"],
            account["used_traffic"], account["status"], account["subscription_url"],
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 تمدید", callback_data=f"d:renew_ask:{account['id']}"),
            InlineKeyboardButton("🗑 حذف", callback_data=f"d:del_ask:{account['id']}"),
        ]])
        await update.effective_message.reply_text(line, reply_markup=keyboard)
    if len(accounts) > len(shown):
        await update.effective_message.reply_text(
            f"…و {texts.fa(len(accounts) - len(shown))} اکانت دیگر — با تیم فروش هماهنگ کنید."
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    if text == texts.MENU_NEW:
        return await _offer_new_account(update)
    if text == texts.MENU_LIST:
        return await _list_accounts(update)
    # Anything else: re-show the menu rather than silently dropping it —
    # same "an ignored message reads as a broken bot" reasoning shopbot uses.
    try:
        session = await _session(update)
    except DelegateApiError:
        await update.effective_message.reply_text(texts.GENERIC_ERROR)
        return
    if session is None:
        await update.effective_message.reply_text(texts.NOT_A_DELEGATE)
        return
    await update.effective_message.reply_text("از دکمه‌های پایین استفاده کنید.", reply_markup=main_menu())


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "new":
        gb = float(parts[2])
        try:
            account = await backend.post("/api/delegate/bot/accounts", json={
                "telegram_id": update.effective_user.id, "data_limit_gb": gb,
            })
        except DelegateApiError as exc:
            await query.edit_message_text(str(exc) if exc.status == 400 else texts.GENERIC_ERROR)
            return
        # default_duration_days isn't in DelegateAccountRow — re-read it from
        # the session rather than threading it through the callback data,
        # which would otherwise grow with every field a future edit needs.
        session = await _session(update)
        duration = session["default_duration_days"] if session else 30
        await query.edit_message_text(texts.account_created(account["marzban_username"], gb, duration))
        return

    if action == "renew_ask":
        account_id = int(parts[2])
        session = await _session(update)
        if session is None:
            await query.edit_message_text(texts.NOT_A_DELEGATE)
            return
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"+{g}GB", callback_data=f"d:renew:{account_id}:{g}") for g in session["quick_volumes_gb"]
        ]])
        await query.message.reply_text(texts.pick_volume_for_renew(), reply_markup=keyboard)
        return

    if action == "renew":
        account_id, gb = int(parts[2]), float(parts[3])
        try:
            account = await backend.post(
                f"/api/delegate/bot/accounts/{account_id}/renew",
                json={"telegram_id": update.effective_user.id, "extend_gb": gb},
            )
        except DelegateApiError as exc:
            await query.edit_message_text(str(exc) if exc.status == 400 else texts.GENERIC_ERROR)
            return
        session = await _session(update)
        duration = session["default_duration_days"] if session else 30
        await query.edit_message_text(texts.renewed(account["marzban_username"], gb, duration))
        return

    if action == "del_ask":
        account_id = int(parts[2])
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"d:del:{account_id}"),
            InlineKeyboardButton("❌ انصراف", callback_data=f"d:del_cancel:{account_id}"),
        ]])
        # Reuses the account's own username from the ORIGINAL list message
        # (query.message.text starts with the status line built by
        # texts.account_line) so this confirm names the right account
        # without a second backend round trip.
        username = query.message.text.split(" ")[1] if query.message.text else "?"
        await query.edit_message_text(texts.confirm_delete(username), reply_markup=keyboard)
        return

    if action == "del_cancel":
        await query.edit_message_text(texts.delete_cancelled())
        return

    if action == "del":
        account_id = int(parts[2])
        username = query.message.text.split("«", 1)[-1].split("»", 1)[0] if query.message.text else "?"
        try:
            await backend.post(
                f"/api/delegate/bot/accounts/{account_id}/delete",
                json={"telegram_id": update.effective_user.id},
            )
        except DelegateApiError as exc:
            await query.edit_message_text(str(exc) if exc.status == 400 else texts.GENERIC_ERROR)
            return
        await query.edit_message_text(texts.deleted(username))
        return


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)
