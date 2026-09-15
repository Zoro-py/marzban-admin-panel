import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# Windows consoles default to a legacy codepage (e.g. cp1252), which raises
# UnicodeEncodeError the moment a log line contains a non-ASCII character
# (Persian text, "∞", "•", …). Force UTF-8 so logging never crashes on this.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters  # noqa: E402

from handlers.account import extend_command  # noqa: E402
from handlers.backup import backup_command  # noqa: E402
from handlers.bulk import bulk_callback, bulk_command  # noqa: E402
from handlers.customer import charge_command, credit_command, customer_command  # noqa: E402
from handlers.report import report_command  # noqa: E402
from handlers.start import help_command, start_command  # noqa: E402
from handlers.sync import sync_command  # noqa: E402
from handlers.topup import (  # noqa: E402
    approve_by_command,
    reject_by_command,
    topup_callback,
    topups_command,
)
from handlers.wallet import wallet_callback, wallet_command, wallet_find_command  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    token = os.environ["BOT_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("customer", customer_command))
    app.add_handler(CommandHandler("charge", charge_command))
    app.add_handler(CommandHandler("credit", credit_command))
    app.add_handler(CommandHandler("extend", extend_command))
    app.add_handler(CommandHandler("bulk", bulk_command))
    # Pattern-scoped so this handler only ever sees its own buttons — an
    # unscoped CallbackQueryHandler would swallow every future feature's
    # callbacks too, and they would silently stop working.
    app.add_handler(CallbackQueryHandler(bulk_callback, pattern=r"^bulk:"))
    app.add_handler(CommandHandler("topups", topups_command))
    # /approve_12 and /reject_12 are dynamic command names, so CommandHandler
    # (which matches a fixed name) can't see them — a Regex MessageHandler is
    # the only way to catch a command whose id is part of the word.
    app.add_handler(MessageHandler(filters.Regex(r"^/approve_\d+(@\w+)?$"), approve_by_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/reject_\d+(@\w+)?$"), reject_by_command))
    app.add_handler(CallbackQueryHandler(topup_callback, pattern=r"^topup:"))
    app.add_handler(CommandHandler("wallet", wallet_command))
    app.add_handler(CommandHandler("wallet_find", wallet_find_command))
    app.add_handler(CallbackQueryHandler(wallet_callback, pattern=r"^wallet:"))
    app.add_handler(CommandHandler("sync", sync_command))
    app.add_handler(CommandHandler("backup", backup_command))

    app.run_polling()


if __name__ == "__main__":
    main()
