"""The dedicated PAYMENTS bot (owner, 2026-10-24: «فقط فیش‌های واریزی که
باید تأیید کنم اینجا بیاد»).

The backend relays every customer receipt to this bot's token (backend env
RECEIPTS_BOT_TOKEN — see notify.relay_shop_photo_to_admin), so the inline
approve/reject buttons land HERE and this process answers them. Everything
else the operator bot does (accounts, sync, alarms…) stays in bot/; the
operator bot's own topup handlers keep working for old messages in that chat.

Trust boundary, same reasoning as the other two bots: this process holds the
operator's Marzban login (it can approve money) but polls a token whose
ENTIRE message history is payments — a clean, single-purpose inbox.
"""

import logging
import os
import sys
import threading

from dotenv import load_dotenv

load_dotenv()

# Windows consoles default to a legacy codepage, which raises
# UnicodeEncodeError the moment a log line contains Persian text (same guard
# as the other bots).
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
from telegram.request import HTTPXRequest  # noqa: E402

from handlers.topup import (  # noqa: E402
    approve_by_command,
    reject_by_command,
    start,
    topup_callback,
    topups_command,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def on_error(update, context) -> None:
    """Without this, an unexpected exception leaves the operator with NO reply
    on a money decision — the same rule as the other bots."""
    logger.exception("Unhandled error in the receipts bot", exc_info=context.error)


async def _register_commands(app) -> None:
    from telegram import BotCommand
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "راهنما"),
            BotCommand("topups", "رسیدهای در انتظار تأیید"),
        ])
    except Exception:
        logger.warning("set_my_commands failed — keeping the previous menu", exc_info=True)


def main() -> None:
    token = os.environ.get("RECEIPTS_BOT_TOKEN", "").strip()
    if not token:
        # Like the shop bot: an unconfigured receipts bot must not restart-loop
        # forever — it waits, visibly idle, until a token is set.
        logger.warning("RECEIPTS_BOT_TOKEN is empty — the receipts bot stays idle.")
        threading.Event().wait()
        return
    if not os.environ.get("ADMIN_CHAT_ID", "").strip():
        logger.warning("ADMIN_CHAT_ID is empty — every handler would be gated off. Set it in receipts_bot/.env.")

    # The same wider timeouts as the shop bot (2026-10-22): this host's path
    # to Telegram showed transient blips under PTB's 5s defaults.
    request = HTTPXRequest(connect_timeout=10.0, read_timeout=20.0, write_timeout=20.0, pool_timeout=10.0)
    app = Application.builder().token(token).request(request).post_init(_register_commands).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("topups", topups_command))
    app.add_handler(CallbackQueryHandler(topup_callback, pattern=r"^topup:"))
    app.add_handler(MessageHandler(filters.Regex(r"^/approve_\d+(@\w+)?$"), approve_by_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/reject_\d+(@\w+)?$"), reject_by_command))
    app.add_error_handler(on_error)

    logger.info("Receipts bot starting")
    app.run_polling()


if __name__ == "__main__":
    main()
