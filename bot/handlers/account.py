from telegram import Update
from telegram.ext import ContextTypes

from api_client import backend
from handlers.common import admin_only, format_expire, format_gb, resolve_account


@admin_only
async def extend_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/extend <username> <days>` (negative days to reduce), "
            "or `/extend <username> <days> <gb>` to also adjust the data limit.",
            parse_mode="Markdown",
        )
        return

    username, days_str = context.args[0], context.args[1]
    gb_str = context.args[2] if len(context.args) > 2 else None

    try:
        days = int(days_str)
        gb = float(gb_str) if gb_str is not None else None
    except ValueError:
        await update.message.reply_text("Days and GB must be numbers.")
        return

    account = await resolve_account(username)
    if account is None:
        await update.message.reply_text(f"No tracked account named `{username}`.", parse_mode="Markdown")
        return

    body = {"extend_days": days}
    if gb is not None:
        body["extend_gb"] = gb

    try:
        updated = await backend.post(f"/api/accounts/{account['id']}/adjust", json=body)
    except ValueError as exc:
        await update.message.reply_text(f"Failed: {exc}")
        return

    await update.message.reply_text(
        f"Updated `{updated['marzban_username']}` — expires {format_expire(updated['expire'])}, "
        f"limit {format_gb(updated['data_limit'])}",
        parse_mode="Markdown",
    )
