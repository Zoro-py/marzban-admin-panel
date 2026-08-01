from telegram import Update
from telegram.ext import ContextTypes

from api_client import backend
from handlers.common import admin_only


@admin_only
async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Backing up and sending the file to this chat…")
    try:
        result = await backend.post("/api/backup/run")
    except ValueError as exc:
        await update.message.reply_text(f"Backup failed: {exc}")
        return
    await update.message.reply_text(f"Backup sent: {result['filename']}")
