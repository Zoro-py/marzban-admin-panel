"""Operator commands to grant/list/revoke delegated self-service access —
see backend/app/models.py's Delegate docstring for what the grant actually
allows.

/delegate_add uses the SAME fuzzy resolve_customer() as /charge and
/customer, but — UNLIKE those — a misresolve here is not a small, visible,
correctable mistake. /charge's worst case is one wrong ledger line the
operator can see and fix; granting the wrong customer delegate access hands
a stranger direct create/renew/delete control (and auto-charging) over
ANOTHER customer's whole account fleet, silently, until someone notices.
So: resolve by fuzzy name same as always, but show exactly who was matched
— name, id, how many accounts they already have — and require an explicit
Confirm tap before the grant is written. Same reasoning as
bot/handlers/wallet.py's confirm step for /wallet, applied to a different
kind of high-blast-radius mistake.

credit_limit is intentionally NOT a /delegate_add argument. It used to be a
trailing optional token, which was ambiguous for any customer name ending
in a digit and, combined with the backend's old "overwrite every field"
upsert, meant re-running /delegate_add to fix a typo silently reset an
existing credit_limit to unlimited. It's now its own command
(/delegate_cap) that only ever touches that one field.
"""

from __future__ import annotations

import itertools
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from api_client import backend
from handlers.common import AmbiguousMatch, admin_only, format_toman, md, resolve_customer

# In-memory, per-process — same "popped not read" reasoning as
# bot/handlers/wallet.py's _pending: a pending grant is a few small fields,
# lives for at most a few seconds until Confirm/Cancel, and this bot is
# gated to one chat id, so there's no multi-operator/multi-process case
# that needs this to survive a restart.
_pending: dict[int, dict] = {}
_next_token = itertools.count(1)


async def _reply_ambiguous(update: Update, matches: list[dict]) -> None:
    lines = ["Multiple customers match — retry with the numeric id:"]
    for c in matches:
        lines.append(f"• #{c['id']} — {c['name']}")
    await update.message.reply_text("\n".join(lines))


def _parse_telegram_id(raw: str) -> Optional[int]:
    """Strict: digits only (optionally one leading '-'), rejecting anything
    int() would otherwise half-accept ('--5', '1_000', leading/trailing
    junk) that could silently target the wrong chat id."""
    body = raw[1:] if raw.startswith("-") else raw
    if not body.isdigit():
        return None
    return int(raw)


@admin_only
async def delegate_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/delegate_add <telegram_id> <customer name or id>`\n"
            "Grants that customer's own Telegram account direct self-service "
            "(create/renew/delete their OWN accounts) via delegate_bot — never "
            "any visibility into their balance or the ledger. Shows you who "
            "was matched before anything is granted.\n"
            "Set a credit limit with /delegate_cap once the grant exists.",
            parse_mode="Markdown",
        )
        return

    telegram_id = _parse_telegram_id(context.args[0])
    if telegram_id is None:
        await update.message.reply_text("The first argument must be the numeric telegram_id.")
        return

    query = " ".join(context.args[1:])
    try:
        customer = await resolve_customer(query)
    except AmbiguousMatch as exc:
        await _reply_ambiguous(update, exc.matches)
        return
    if customer is None:
        await update.message.reply_text(f"No customer matches '{query}'.")
        return

    try:
        existing_accounts = await backend.get(f"/api/customers/{customer['id']}/accounts")
        account_count = len(existing_accounts)
    except Exception:  # noqa: BLE001 — a count failure shouldn't block showing the confirm
        account_count = None

    token = next(_next_token)
    _pending[token] = {"customer_id": customer["id"], "telegram_id": telegram_id}

    count_line = f"{account_count} existing account(s)" if account_count is not None else "account count unavailable"
    text = (
        f"Grant delegate access to *{md(customer['name'])}* (id `{customer['id']}`, {count_line})\n"
        f"for Telegram id `{telegram_id}`?\n\n"
        "They will be able to create/renew/delete their OWN accounts — never see money.\n"
        "Wrong customer? Cancel and retry with the numeric customer id instead of a name."
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data=f"delegate:ok:{token}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"delegate:no:{token}"),
    ]])
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


@admin_only
async def delegate_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, action, raw_token = query.data.split(":", 2)
        token = int(raw_token)
    except (ValueError, AttributeError):
        await query.edit_message_text("This button is malformed — start over with /delegate_add.")
        return

    # Popped, not read: a second tap must not be able to grant twice.
    pending = _pending.pop(token, None)
    if pending is None:
        await query.edit_message_text("This confirmation already expired or was used — start over with /delegate_add.")
        return

    original = query.message.text or ""
    if action == "no":
        await query.edit_message_text(f"{original}\n\n❌ Cancelled — nothing granted.", reply_markup=None, parse_mode="Markdown")
        return
    if action != "ok":
        await query.edit_message_text("Unrecognised button — start over with /delegate_add.")
        return

    try:
        await backend.post("/api/delegate", json={
            "customer_id": pending["customer_id"],
            "telegram_id": pending["telegram_id"],
        })
    except Exception as exc:  # noqa: BLE001
        await query.edit_message_text(f"{original}\n\n❌ Failed — {md(str(exc))}", reply_markup=None, parse_mode="Markdown")
        return

    await query.edit_message_text(
        f"{original}\n\n✅ Granted. They can now message delegate_bot.",
        reply_markup=None,
        parse_mode="Markdown",
    )


@admin_only
async def delegate_cap_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 2:
        await update.message.reply_text(
            "Usage: `/delegate_cap <telegram_id> <amount|none>`\n"
            "Sets (or, with `none`, clears — unlimited trust) an existing "
            "delegate's credit limit. Doesn't touch anything else about the grant.",
            parse_mode="Markdown",
        )
        return
    telegram_id = _parse_telegram_id(context.args[0])
    if telegram_id is None:
        await update.message.reply_text("The first argument must be the numeric telegram_id.")
        return

    raw_limit = context.args[1]
    credit_limit: Optional[float]
    if raw_limit.lower() == "none":
        credit_limit = None
    else:
        try:
            credit_limit = float(raw_limit.replace(",", ""))
        except ValueError:
            await update.message.reply_text(f"Amount must be a number, or 'none' — got '{raw_limit}'.")
            return
        # float() accepts 'inf'/'nan', which would silently mean "no limit"
        # while LOOKING like a real bounded number in /delegate_list.
        if credit_limit != credit_limit or credit_limit in (float("inf"), float("-inf")) or credit_limit < 0:
            await update.message.reply_text("Amount must be a finite number >= 0.")
            return

    try:
        delegates = await backend.get("/api/delegate")
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"Could not read delegates: {exc}")
        return
    match = next((d for d in delegates if d["telegram_id"] == telegram_id), None)
    if match is None:
        await update.message.reply_text(f"No delegate with telegram_id {telegram_id} — see /delegate_add.")
        return

    try:
        await backend.post("/api/delegate", json={"telegram_id": telegram_id, "credit_limit": credit_limit})
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"Could not update: {exc}")
        return

    cap_line = format_toman(credit_limit) if credit_limit is not None else "no limit (unlimited trust)"
    await update.message.reply_text(f"✅ Credit limit for {md(match['scope_name'])} set to {cap_line}.", parse_mode="Markdown")


@admin_only
async def delegate_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        delegates = await backend.get("/api/delegate")
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"Could not read delegates: {exc}")
        return
    if not delegates:
        await update.message.reply_text("No delegates yet — see /delegate_add.")
        return
    lines = [f"{len(delegates)} delegate(s):"]
    for d in delegates:
        status = "✅" if d["is_active"] else "⛔"
        cap = format_toman(d["credit_limit"]) if d["credit_limit"] is not None else "no limit"
        lines.append(f"{status} `{d['telegram_id']}` — {md(d['scope_name'])} (cap {cap}, {d['daily_create_cap']}/day)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@admin_only
async def delegate_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /delegate_off <telegram_id>")
        return
    telegram_id = _parse_telegram_id(context.args[0])
    if telegram_id is None:
        await update.message.reply_text("Usage: /delegate_off <telegram_id>")
        return
    try:
        delegates = await backend.get("/api/delegate")
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"Could not read delegates: {exc}")
        return
    match = next((d for d in delegates if d["telegram_id"] == telegram_id), None)
    if match is None:
        await update.message.reply_text(f"No delegate with telegram_id {telegram_id}.")
        return
    try:
        await backend.post(f"/api/delegate/{match['id']}/deactivate")
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"Could not deactivate: {exc}")
        return
    await update.message.reply_text(f"⛔ Delegate access revoked for {md(match['scope_name'])} (id {telegram_id}).",
                                    parse_mode="Markdown")
