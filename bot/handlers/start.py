from telegram import Update
from telegram.ext import ContextTypes

from handlers.common import admin_only

HELP_TEXT = """*VPN reseller bot*

/report — daily summary: overdue customers, near-quota, expiring soon, unassigned
/customer <name or id> — balance + accounts for one customer
/charge <customer> <amount> [note] — record a debt (بدهی)
/credit <customer> <amount> [note] — record a credit/payment (طلب)
/extend <username> <days> [gb] — extend or reduce time (and optionally data) on a Marzban account
/sync — pull the latest usage/status from Marzban now
"""


@admin_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


@admin_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")
