"""Operator-side handling of shop top-up requests.

The buttons these respond to are attached by the BACKEND
(routers/shop.py::_alert_operator_to_topup), sent with this bot's own token —
which is why the taps arrive here even though nothing in this process sent the
message. That indirection is deliberate: the alert has to go out the moment a
customer uploads a receipt, and the backend is the only part that is
guaranteed to be running at that moment.

Approving is the single most money-sensitive action in this bot: it creates
balance out of nothing on the operator's say-so. The backend refuses to
approve a top-up that isn't still pending, so a double-tap credits once — but
this file must never work around that guard by, say, re-approving on a
"already approved" error.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from api_client import backend
from handlers.common import admin_only, format_toman


@admin_only
async def topups_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists receipts still waiting. Exists as a fallback for when the
    original notification was missed, deleted, or its buttons no longer work
    — without it, a receipt that failed to alert would sit unreviewed with
    nothing in Telegram pointing at it."""
    try:
        pending = await backend.get("/api/shop/topups", params={"status": "pending"})
    except Exception as exc:  # noqa: BLE001 — the operator needs the reason
        await update.message.reply_text(f"Couldn't read pending top-ups: {exc}")
        return

    if not pending:
        await update.message.reply_text("No top-ups waiting for review.")
        return

    lines = [f"{len(pending)} top-up(s) waiting:", ""]
    for topup in pending[:20]:
        who = topup.get("display_name") or topup.get("telegram_id") or "unknown"
        # The code is what the customer will quote; the order marker says
        # whether approving also DELIVERS a plan (and so whether approving
        # less than the price leaves someone waiting for one).
        code = f" [{topup['reference_code']}]" if topup.get("reference_code") else ""
        kind = f" · for order #{topup['order_id']}" if topup.get("order_id") else " · wallet only"
        lines.append(
            f"#{topup['id']}{code} — {who} — {format_toman(topup['claimed_amount'])}{kind}\n"
            f"   /approve_{topup['id']}   /reject_{topup['id']}"
        )
    if len(pending) > 20:
        lines.append(f"…and {len(pending) - 20} more (see the dashboard's Shop page)")
    await update.message.reply_text("\n".join(lines))


async def _decide(topup_id: int, approve: bool) -> str:
    """Returns the line to show the operator. Never raises — both callers are
    Telegram handlers where an exception means the operator sees nothing at
    all and cannot tell whether the money moved."""
    action = "approve" if approve else "reject"
    try:
        result = await backend.post(f"/api/shop/topups/{topup_id}/{action}", json={})
    except ValueError as exc:
        # The backend's own refusal, e.g. "This top-up was already approved."
        # Shown verbatim: that sentence is exactly what the operator needs.
        return f"⚠️ #{topup_id}: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"❌ #{topup_id}: couldn't reach the backend — {exc}"

    if approve:
        amount = result.get("approved_amount") or result.get("claimed_amount") or 0
        return f"✅ #{topup_id} approved — {format_toman(amount)} credited."
    return f"❌ #{topup_id} rejected. The customer has been told."


@admin_only
async def topup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    try:
        _, action, raw_id = query.data.split(":", 2)
        topup_id = int(raw_id)
    except (ValueError, AttributeError):
        await query.edit_message_caption(caption="This button is malformed — use /topups instead.")
        return

    outcome = await _decide(topup_id, approve=(action == "ok"))

    # The alert is usually a PHOTO (the receipt), so its text lives in the
    # caption, not the message body — edit_message_text would fail on it. The
    # buttons are dropped either way so the decision can't be re-tapped; the
    # backend would refuse a repeat anyway, but leaving live buttons on a
    # settled request invites the operator to wonder whether it took.
    original = (query.message.caption if query.message.caption is not None else query.message.text) or ""
    new_text = f"{original}\n\n{outcome}"
    try:
        if query.message.caption is not None:
            await query.edit_message_caption(caption=new_text, reply_markup=None)
        else:
            await query.edit_message_text(text=new_text, reply_markup=None)
    except Exception:
        # Editing can fail (message too old to edit, identical content). The
        # outcome still has to reach the operator, so say it as a new message
        # rather than losing it.
        await query.message.reply_text(outcome)


@admin_only
async def approve_by_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Backs the /approve_<id> shortcuts printed by /topups."""
    topup_id = _id_from_command(update.message.text, "approve")
    if topup_id is None:
        await update.message.reply_text("Usage: /approve_<id> — see /topups for the list.")
        return
    await update.message.reply_text(await _decide(topup_id, approve=True))


@admin_only
async def reject_by_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    topup_id = _id_from_command(update.message.text, "reject")
    if topup_id is None:
        await update.message.reply_text("Usage: /reject_<id> — see /topups for the list.")
        return
    await update.message.reply_text(await _decide(topup_id, approve=False))


def _id_from_command(text: str | None, verb: str) -> int | None:
    """Parses "/approve_12" and "/approve_12@SomeBot" — Telegram appends the
    bot's username to commands sent in groups, and the operator's bot may well
    be used in one."""
    if not text:
        return None
    head = text.strip().split()[0].split("@")[0]
    prefix = f"/{verb}_"
    if not head.startswith(prefix):
        return None
    try:
        return int(head[len(prefix):])
    except ValueError:
        return None
