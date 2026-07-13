from telegram import Update
from telegram.ext import ContextTypes

from api_client import backend
from handlers.common import admin_only


@admin_only
async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Syncing with Marzban…")
    try:
        result = await backend.post("/api/sync/run")
    except ValueError as exc:
        await update.message.reply_text(f"Sync failed: {exc}")
        return
    await update.message.reply_text(
        f"Synced {result['marzban_user_count']} users — {result['created']} new, {result['updated']} updated."
    )
