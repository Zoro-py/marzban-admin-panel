"""Delegate self-service bot.

Separate process and separate Telegram token from both bot/ (the operator's
own single-chat admin bot, holding the Marzban admin credentials) and
shopbot/ (the public shop). This one is neither: its audience is a small,
explicitly-granted list of trusted customers (see backend/app/models.py's
Delegate docstring), but it still runs as its own process holding only
DELEGATE_BOT_API_KEY — never the admin credentials, never the wallet/charge
endpoints — on the same "bound what a compromise costs" reasoning
shopbot/api_client.py documents for itself.
"""

import logging
import os
import sys
import threading

from dotenv import load_dotenv

load_dotenv()

# Windows consoles default to a legacy codepage, which raises
# UnicodeEncodeError the moment a log line contains Persian text. Same guard
# as bot/bot.py and shopbot/bot.py.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from telegram.ext import (  # noqa: E402
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from handlers.delegate import handle_callback, handle_text, help_command, start  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def on_error(update, context) -> None:
    """Last line of defence — same reasoning as shopbot's own on_error: an
    unexpected exception in a handler must not leave the delegate with no
    reply at all, especially mid create/renew/delete."""
    logger.exception("Unhandled error while processing an update", exc_info=context.error)
    try:
        if update is not None and getattr(update, "effective_message", None) is not None:
            import texts
            await update.effective_message.reply_text(texts.GENERIC_ERROR)
    except Exception:
        logger.exception("Could not even send the fallback error message")


def main() -> None:
    token = os.environ.get("DELEGATE_BOT_TOKEN", "").strip()
    if not token:
        # Same "stay idle, don't restart-loop" reasoning as shopbot/bot.py:
        # the container always starts under compose, whether or not any
        # delegate has been granted access yet.
        logger.warning("DELEGATE_BOT_TOKEN is empty - delegate_bot stays idle. "
                       "Set it in delegate_bot/.env and restart to enable it.")
        threading.Event().wait()
        return

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^d:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)

    logger.info("Delegate bot starting")
    app.run_polling()


if __name__ == "__main__":
    main()
