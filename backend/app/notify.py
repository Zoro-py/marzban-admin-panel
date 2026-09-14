"""Sends a message to the operator's Telegram chat directly via the Bot API
— not through the separate `bot/` process, which only handles incoming
commands. Shared by every backend feature that needs to tell the operator
something happened (or is about to): sync_job's next-plan auto-queue and
activation, payg_monthly_job's monthly settlement and cap-hit reset.
"""

import asyncio
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def notify_admin(text: str) -> None:
    """Raises on any failure to send — deliberately NOT best-effort, because
    some callers gate a real state change on this succeeding (queuing a next
    plan, or settling+resetting a payg account/group): an operator who never
    sees the notification never gets the chance to review it before the
    change silently applies, indistinguishable from it just happening with
    nobody aware. Callers that want best-effort semantics (e.g. a
    notification that follows an already-committed, already-approved
    action) wrap this in their own try/except — that's the right layer for
    it to be swallowed, not silently inside this function where a caller
    that DOES need to know would never find out."""
    if not settings.bot_token or not settings.bot_admin_chat_id:
        raise RuntimeError("BOT_TOKEN/BOT_ADMIN_CHAT_ID not set — nowhere to send this notification")
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{settings.bot_token}/sendMessage",
            data={"chat_id": settings.bot_admin_chat_id, "text": text},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Telegram rejected admin notification ({resp.status_code}): {resp.text}")


# Telegram's documented cap for a photo caption. Exceeding it fails the whole
# sendPhoto call with a 400, which for a bulk batch would mean losing the QR
# for that account entirely over a few characters of note text.
TELEGRAM_CAPTION_LIMIT = 1024
# Telegram rate-limits bursts to a single chat. A 30-account batch sending as
# fast as httpx can go reliably trips 429 partway through, so callers that
# send in a loop pause this long between messages. Deliberately conservative:
# a batch taking 20 extra seconds is invisible to the operator, a half-sent
# batch is not.
SEND_SPACING_SECONDS = 0.6
# How many times a single send retries after a 429 before giving up. Telegram
# tells us exactly how long to wait (`retry_after`), so this is a bound on
# pathological cases, not a guess at the right delay.
MAX_RATE_LIMIT_RETRIES = 3


def _truncate_caption(caption: str) -> str:
    if len(caption) <= TELEGRAM_CAPTION_LIMIT:
        return caption
    return caption[: TELEGRAM_CAPTION_LIMIT - 1] + "…"


async def notify_admin_photo(photo: bytes, caption: str, *, filename: str = "qr.png") -> None:
    """Sends one photo with a caption to the operator's chat.

    Same raise-on-failure contract as notify_admin above, for the same reason
    — a caller that gates something on delivery has to be able to tell. Callers
    sending a batch (bulk account creation) catch this per item so one failed
    send doesn't abort the rest, and report the failures back to the operator.

    No parse_mode on purpose. Marzban usernames legitimately contain
    underscores, which Markdown treats as italics — `khanevade_1` would either
    render mangled or make Telegram reject the whole message as unparseable
    entities. Telegram auto-links a bare URL in plain text anyway, so the
    formatting buys nothing and costs an entire class of escaping bugs.
    """
    if not settings.bot_token or not settings.bot_admin_chat_id:
        raise RuntimeError("BOT_TOKEN/BOT_ADMIN_CHAT_ID not set — nowhere to send this notification")

    url = f"https://api.telegram.org/bot{settings.bot_token}/sendPhoto"
    data = {"chat_id": settings.bot_admin_chat_id, "caption": _truncate_caption(caption)}

    async with httpx.AsyncClient(timeout=60) as client:
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            resp = await client.post(
                url,
                data=data,
                files={"photo": (filename, photo, "image/png")},
            )
            if resp.status_code == 200:
                return
            if resp.status_code == 429 and attempt < MAX_RATE_LIMIT_RETRIES:
                # Telegram states the required wait itself; honour it rather
                # than backing off on a schedule of our own invention.
                retry_after = 1.0
                try:
                    retry_after = float(resp.json()["parameters"]["retry_after"])
                except Exception:
                    pass
                logger.warning("Telegram rate-limited a photo send; retrying in %.1fs", retry_after)
                await asyncio.sleep(min(retry_after, 60.0) + 0.5)
                continue
            raise RuntimeError(f"Telegram rejected admin photo ({resp.status_code}): {resp.text}")


async def notify_admin_with_buttons(text: str, reply_markup: dict) -> None:
    """Same channel as notify_admin, plus an inline keyboard.

    The message is sent with the OPERATOR's bot token, so the taps come back
    to the operator's own bot process — which is what lets a top-up be
    approved from the notification itself instead of the operator having to go
    find it. `reply_markup` is Telegram's own JSON structure, passed through
    as-is rather than wrapped in a builder: this module has no telegram
    library dependency and should not grow one for a single dict.
    """
    if not settings.bot_token or not settings.bot_admin_chat_id:
        raise RuntimeError("BOT_TOKEN/BOT_ADMIN_CHAT_ID not set — nowhere to send this notification")
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{settings.bot_token}/sendMessage",
            json={
                "chat_id": settings.bot_admin_chat_id,
                "text": _truncate_caption(text),
                "reply_markup": reply_markup,
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Telegram rejected admin notification ({resp.status_code}): {resp.text}")


async def forward_photo_to_admin(file_id: str, caption: str, reply_markup: dict | None = None) -> None:
    """Re-sends a photo the operator's bot can already see, by file_id.

    Used for payment receipts. Telegram file_ids are per-bot, so this only
    works because the SHOP bot forwards the id to the backend and the backend
    re-sends with the OPERATOR's token — which Telegram allows for a photo the
    operator's bot is being asked to send by id only if that bot has seen it.
    When it hasn't, Telegram answers with an error and the caller falls back
    to a text-only notification rather than losing the alert entirely.
    """
    if not settings.bot_token or not settings.bot_admin_chat_id:
        raise RuntimeError("BOT_TOKEN/BOT_ADMIN_CHAT_ID not set — nowhere to send this notification")
    payload = {
        "chat_id": settings.bot_admin_chat_id,
        "photo": file_id,
        "caption": _truncate_caption(caption),
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{settings.bot_token}/sendPhoto",
            json=payload,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Telegram rejected the receipt photo ({resp.status_code}): {resp.text}")


async def send_to_shop_user(chat_id: int, text: str) -> None:
    """Message a SHOP customer directly from the backend, using the shop bot's
    own token. Needed because a purchase is provisioned inside an HTTP request
    from the bot — the reply the customer gets for their tap is the bot's job,
    but anything the backend decides afterwards (a refund, an operator's
    approval landing) has no open request to ride back on."""
    if not settings.shop_bot_token:
        raise RuntimeError("SHOP_BOT_TOKEN not set — nowhere to send this customer message")
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{settings.shop_bot_token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Telegram rejected the customer message ({resp.status_code}): {resp.text}")


async def send_photo_to_shop_user(chat_id: int, photo: bytes, caption: str, *, filename: str = "qr.png") -> None:
    """The shop-bot equivalent of notify_admin_photo — delivers a purchased
    account's QR to the buyer. Shares notify_admin_photo's rate-limit handling
    reasoning but targets one customer at a time, so it does not need the
    batch spacing."""
    if not settings.shop_bot_token:
        raise RuntimeError("SHOP_BOT_TOKEN not set — nowhere to send this customer message")
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{settings.shop_bot_token}/sendPhoto",
            data={"chat_id": chat_id, "caption": _truncate_caption(caption)},
            files={"photo": (filename, photo, "image/png")},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Telegram rejected the customer photo ({resp.status_code}): {resp.text}")
