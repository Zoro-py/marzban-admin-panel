from telegram import Update
from telegram.ext import ContextTypes

from handlers.common import admin_only

HELP_TEXT = """*VPN reseller bot*

/report — daily summary: overdue customers, exhausted/near-quota, expired/expiring soon, no rate configured
/customer <name or id> — balance + accounts for one customer
/charge <customer> <amount> [note] — record a debt (بدهی)
/credit <customer> <amount> [note] — record a credit/payment (طلب)
/extend <username> <days> [gb] — extend or reduce time (and optionally data) on a Marzban account
/bulk <name> <count> [30gb] [30d] [from=N] — create a family batch (name1, name2, …); shows the exact usernames and waits for your confirmation, then sends a QR + link for each
/topups — payment receipts from shop customers waiting for your approval (you also get each one pushed here with approve/reject buttons the moment it arrives)
/wallet_find <name> — look up a shop customer's telegram_id + balance by name/username
/wallet <telegram_id> <amount> <note> — manually credit (+) or debit (-) a shop customer's wallet; asks you to confirm before it moves anything
/delegate_add <telegram_id> <customer> [credit_limit] — let a trusted customer self-manage their own accounts (create/renew/delete) via delegate_bot, no wallet/ledger access
/delegate_list — see who has delegate access
/delegate_off <telegram_id> — revoke a delegate's access
/sync — pull the latest usage/status from Marzban now
/backup — back up the database now and send it to this chat (also runs automatically every night)
"""


@admin_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


@admin_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")
