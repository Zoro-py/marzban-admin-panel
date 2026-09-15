"""Customer-facing shop bot.

Separate process and separate Telegram token from bot/, which is the
operator's own single-chat admin bot. Two reasons, both deliberate:

  * bot/ is gated to one chat id because every command it has moves money or
    changes a live panel. This bot must accept anyone, so the two cannot share
    a process without the gate becoming conditional — and a conditional gate
    is one bad `if` away from being no gate.
  * This one holds only SHOP_BOT_API_KEY, which reaches nothing but
    /api/shop/bot/*. bot/ holds the Marzban admin credentials. Keeping the
    public-facing process away from those bounds what a compromise costs.
"""

import logging
import os
import sys
import threading

from dotenv import load_dotenv

load_dotenv()

# Windows consoles default to a legacy codepage, which raises
# UnicodeEncodeError the moment a log line contains Persian text. Same guard
# as bot/bot.py.
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

from handlers.shop import (  # noqa: E402
    handle_contact,
    handle_document,
    handle_other,
    on_renew,
    handle_photo,
    handle_text,
    help_command,
    start,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def on_error(update, context) -> None:
    """Last line of defence. Without it, an unexpected exception in a handler
    leaves the customer with NO reply at all — which reads as "the bot is
    broken" and, mid-purchase, as "did my money disappear?". The real error
    goes to the log; the customer gets a sentence."""
    logger.exception("Unhandled error while processing an update", exc_info=context.error)
    try:
        if update is not None and getattr(update, "effective_message", None) is not None:
            import texts
            await update.effective_message.reply_text(texts.generic_error(None))
    except Exception:
        logger.exception("Could not even send the fallback error message")


def main() -> None:
    token = os.environ.get("SHOP_BOT_TOKEN", "").strip()
    if not token:
        # The container exists because compose always starts it; the SHOP does
        # not exist until someone sets a token. Exiting here would restart-loop
        # forever under `restart: unless-stopped` and fill the logs, so it
        # simply waits, visibly idle, until the operator configures one.
        logger.warning("SHOP_BOT_TOKEN is empty - the shop bot stays idle. "
                       "Set it in shopbot/.env and restart to enable the shop.")
        threading.Event().wait()
        return

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(on_renew, pattern=r"^renew:"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    # A receipt sent as a file is still a receipt; anything else at least gets
    # an answer instead of silence.
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    # Registered before handle_other's catch-all, or a shared contact (from
    # the optional phone-share prompt — see _maybe_offer_phone_share) would
    # fall through to "not understood" instead of being saved.
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    # TEXT & ~COMMAND: an unrecognised /command should fall through to
    # Telegram's own "unknown command" rather than being parsed as a volume.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(
        ~filters.COMMAND & ~filters.TEXT & ~filters.PHOTO & ~filters.Document.ALL & ~filters.StatusUpdate.ALL,
        handle_other,
    ))
    app.add_error_handler(on_error)

    logger.info("Shop bot starting")
    app.run_polling()


if __name__ == "__main__":
    main()
