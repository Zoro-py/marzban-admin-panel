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

from telegram.ext import Application, CommandHandler  # noqa: E402

from handlers.account import extend_command  # noqa: E402
from handlers.customer import charge_command, credit_command, customer_command  # noqa: E402
from handlers.report import report_command  # noqa: E402
from handlers.start import help_command, start_command  # noqa: E402
from handlers.sync import sync_command  # noqa: E402

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
    app.add_handler(CommandHandler("sync", sync_command))

    app.run_polling()


if __name__ == "__main__":
    main()
