"""Sends a message to the operator's Telegram chat directly via the Bot API
— not through the separate `bot/` process, which only handles incoming
commands. Shared by every backend feature that needs to tell the operator
something happened (or is about to): sync_job's next-plan auto-queue and
activation, payg_monthly_job's monthly settlement and cap-hit reset.
"""

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
