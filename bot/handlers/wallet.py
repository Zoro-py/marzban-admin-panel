"""Operator-side manual wallet correction for the self-serve shop.

Deliberately NOT name-matched the way /charge is for the reseller ledger
(see customer.py's resolve_customer). Shop users are the public, and
display_name/telegram_username are customer-controlled — a shop user can
rename themselves to collide with, or be a substring of, another. /charge's
fuzzy match is safe because it searches known business customers the
operator already has a real relationship with; the same trick here would
search strangers, and the worst case — crediting or debiting the WRONG
stranger's wallet — is irreversible the moment it's spent (a wrongly
credited stranger can buy and use a VPN account before anyone notices; a
wrongly debited paying customer has no self-serve way to get it back).

So: exact numeric telegram_id only for the action that actually moves
money, a separate READ-ONLY command to find that id by name (searching is
safe, adjusting isn't), and a confirm step — showing exactly who, how much,
and the resulting balance — before anything is sent to the backend.
"""

from __future__ import annotations

import itertools

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from api_client import backend
from handlers.common import admin_only, format_toman, md

# In-memory only, by design: a pending adjustment is a few small fields,
# lives for at most a few seconds (until the operator taps Confirm or
# Cancel), and this bot is gated to exactly one chat id (admin_only) — there
# is no multi-operator or multi-process case that needs this to survive a
# restart or be shared anywhere.
_pending: dict[int, dict] = {}
_next_token = itertools.count(1)


async def _find_shop_user(telegram_id: int) -> dict | None:
    users = await backend.get("/api/shop/users")
    for u in users:
        if u["telegram_id"] == telegram_id:
            return u
    return None


@admin_only
async def wallet_find_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Read-only lookup by name/username fragment — the safe half of this
    file. Never used to decide WHO gets credited; only to find the id that
    /wallet then requires exactly."""
    if not context.args:
        await update.message.reply_text("Usage: /wallet_find <name or username fragment>")
        return
    query = " ".join(context.args).lower()
    try:
        users = await backend.get("/api/shop/users")
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"Couldn't read shop users: {exc}")
        return
    matches = [
        u for u in users
        if query in (u.get("display_name") or "").lower()
        or query in (u.get("telegram_username") or "").lower()
    ]
    if not matches:
        await update.message.reply_text(f"No shop user matches '{query}'.")
        return
    lines = [f"{len(matches)} match(es):"]
    for u in matches[:20]:
        who = md(u.get("display_name") or u.get("telegram_username") or "unknown")
        lines.append(f"• `{u['telegram_id']}` — {who} — balance {format_toman(u['balance'])}")
    if len(matches) > 20:
        lines.append(f"…and {len(matches) - 20} more — narrow the search.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@admin_only
async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: `/wallet <telegram_id> <amount> <note>`\n"
            "Positive credits, negative debits. A note is required — this "
            "moves a stranger's balance with no receipt behind it.\n"
            "Don't know the id? Try `/wallet_find <name>` first.",
            parse_mode="Markdown",
        )
        return

    raw_id, raw_amount, *note_parts = context.args
    note = " ".join(note_parts).strip()
    if not note:
        await update.message.reply_text("A note is required — say why this balance is changing.")
        return

    if not raw_id.lstrip("-").isdigit():
        await update.message.reply_text("The first argument must be the numeric telegram_id — see /wallet_find.")
        return
    telegram_id = int(raw_id)

    try:
        amount = int(raw_amount.replace(",", ""))
    except ValueError:
        await update.message.reply_text(f"Amount must be a whole number of Toman — got '{raw_amount}'.")
        return
    if amount == 0:
        await update.message.reply_text("Amount can't be zero.")
        return

    try:
        user = await _find_shop_user(telegram_id)
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"Couldn't look up that user: {exc}")
        return
    if user is None:
        await update.message.reply_text(f"No shop user with telegram_id {telegram_id}. Check /wallet_find.")
        return

    # Courtesy warning only, never a block: a manual correction sometimes
    # legitimately has to be large (refunding a mistaken big credit). Same
    # reasoning as the schema's own comment (ShopWalletAdjustRequest).
    warn = ""
    try:
        settings = await backend.get("/api/shop/settings")
        if settings.get("max_topup", 0) and abs(amount) > settings["max_topup"]:
            warn = "\n⚠️ Larger than the shop's own max top-up — double check the amount."
    except Exception:  # noqa: BLE001
        pass

    token = next(_next_token)
    _pending[token] = {"user_id": user["id"], "amount": amount, "note": note}

    who = md(user.get("display_name") or user.get("telegram_username") or "unknown")
    resulting = user["balance"] + amount
    direction = "Credit" if amount > 0 else "Debit"
    text = (
        f"{direction} {format_toman(abs(amount))} for *{who}* (id `{telegram_id}`)\n"
        f"Note: {md(note)}\n"
        f"Balance: {format_toman(user['balance'])} → {format_toman(resulting)}"
        f"{warn}\n\nConfirm?"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data=f"wallet:ok:{token}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"wallet:no:{token}"),
    ]])
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


@admin_only
async def wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, action, raw_token = query.data.split(":", 2)
        token = int(raw_token)
    except (ValueError, AttributeError):
        await query.edit_message_text("This button is malformed — start over with /wallet.")
        return

    # Popped, not read: a second tap on the same button (double-tap, or a
    # slow network delivering it twice) must not be able to replay the same
    # adjustment — the backend has no idempotency key here to catch it.
    pending = _pending.pop(token, None)
    if pending is None:
        await query.edit_message_text("This confirmation already expired or was used — start over with /wallet.")
        return

    # The confirmation message was sent with parse_mode=Markdown (it quotes
    # the customer's own note and name), and an edit does not inherit that —
    # it has to be resupplied, or the asterisks/backticks in `original` show
    # up literally instead of rendering.
    original = query.message.text or ""
    if action == "no":
        await query.edit_message_text(f"{original}\n\n❌ Cancelled — nothing changed.", reply_markup=None, parse_mode="Markdown")
        return
    if action != "ok":
        await query.edit_message_text("Unrecognised button — start over with /wallet.")
        return

    try:
        result = await backend.post(
            f"/api/shop/users/{pending['user_id']}/wallet",
            json={"amount": pending["amount"], "note": pending["note"]},
        )
    except Exception as exc:  # noqa: BLE001 — the operator has to know whether the money moved
        # md()'d: an arbitrary backend error string is exactly the kind of
        # unescaped text that made Telegram reject a whole message before
        # (see md()'s own docstring) — here that would leave the operator
        # not even knowing whether the adjustment went through.
        await query.edit_message_text(f"{original}\n\n❌ Failed — {md(str(exc))}", reply_markup=None, parse_mode="Markdown")
        return

    await query.edit_message_text(
        f"{original}\n\n✅ Done — new balance {format_toman(result['balance'])}.",
        reply_markup=None,
        parse_mode="Markdown",
    )
