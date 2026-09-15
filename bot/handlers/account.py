import itertools

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from api_client import backend
from handlers.common import admin_only, format_expire, format_gb, format_toman, md, resolve_account

# Same "popped, not read" pending-confirm pattern as wallet.py/
# delegate_admin.py — deletion is irreversible (there is no undo
# endpoint), so it gets the same mandatory Confirm/Cancel step those two
# use for anything money-moving or high-blast-radius.
_pending_delete: dict[int, dict] = {}
_next_delete_token = itertools.count(1)


@admin_only
async def extend_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/extend <username> <days>` (negative days to reduce), "
            "or `/extend <username> <days> <gb>` to also adjust the data limit and bill it "
            "at this account's rate (same as the dashboard's Adjust section — days alone are never billed).",
            parse_mode="Markdown",
        )
        return

    username, days_str = context.args[0], context.args[1]
    gb_str = context.args[2] if len(context.args) > 2 else None

    try:
        days = int(days_str)
        gb = float(gb_str) if gb_str is not None else None
    except ValueError:
        await update.message.reply_text("Days and GB must be numbers.")
        return

    account = await resolve_account(username)
    if account is None:
        await update.message.reply_text(f"No tracked account named `{username}`.", parse_mode="Markdown")
        return

    body = {"extend_days": days}
    if gb is not None:
        body["extend_gb"] = gb

    try:
        updated = await backend.post(f"/api/accounts/{account['id']}/adjust", json=body)
    except ValueError as exc:
        await update.message.reply_text(f"Failed: {exc}")
        return

    # This command used to apply the Marzban change and stop — no ledger
    # entry, ever, for any amount of data added. Mirrors the dashboard's own
    # Adjust section instead: GB added is billed at this account's effective
    # rate by default (days-only extensions are never billed — price is per
    # GB, not per day). account is the pre-adjust AccountRow from
    # resolve_account, which already carries customer_id/group_id/
    # effective_rate/rate_configured — none of that changes from an adjust.
    charge_note = ""
    if gb is not None and gb > 0:
        if account.get("customer_id") is None and account.get("group_id") is None:
            charge_note = " (not billed — unassigned account)"
        elif not account.get("rate_configured"):
            charge_note = " (not billed — no rate configured)"
        else:
            amount = round(gb * account["effective_rate"], 2)
            if amount > 0:
                try:
                    await backend.post(
                        "/api/ledger",
                        json={
                            "type": "charge",
                            "amount": amount,
                            "customer_id": account.get("customer_id"),
                            "group_id": account.get("group_id"),
                            "account_id": account["id"],
                            "note": f"+{gb}GB via bot /extend",
                        },
                    )
                    charge_note = f" — charged {format_toman(amount)}"
                except Exception as exc:  # noqa: BLE001
                    # The panel change already happened and cannot be undone
                    # here. Saying "charged" when nothing was recorded is how
                    # traffic ends up given away and never invoiced.
                    charge_note = (f" — ⚠️ panel updated but NOT billed ({exc}). "
                                   f"Add {format_toman(amount)} by hand.")

    await update.message.reply_text(
        f"Updated `{updated['marzban_username']}` — expires {format_expire(updated['expire'])}, "
        f"limit {format_gb(updated['data_limit'])}{charge_note}",
        parse_mode="Markdown",
    )


@admin_only
async def delete_account_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: `/delete_account <username>`\n"
            "Permanently removes the Marzban user — irreversible, no undo. "
            "Asks you to confirm before it actually deletes anything.",
            parse_mode="Markdown",
        )
        return

    username = context.args[0]
    account = await resolve_account(username)
    if account is None:
        await update.message.reply_text(f"No tracked account named `{username}`.", parse_mode="Markdown")
        return

    token = next(_next_delete_token)
    _pending_delete[token] = {"account_id": account["id"], "marzban_username": account["marzban_username"]}

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm delete", callback_data=f"delacc:ok:{token}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"delacc:no:{token}"),
    ]])
    await update.message.reply_text(
        f"⚠️ Permanently delete `{md(account['marzban_username'])}`? This cannot be undone.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


@admin_only
async def delete_account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, action, raw_token = query.data.split(":", 2)
        token = int(raw_token)
    except (ValueError, AttributeError):
        await query.edit_message_text("This button is malformed — start over with /delete_account.")
        return

    pending = _pending_delete.pop(token, None)
    if pending is None:
        await query.edit_message_text("This confirmation already expired or was used — start over with /delete_account.")
        return

    original = query.message.text or ""
    if action == "no":
        await query.edit_message_text(f"{original}\n\n❌ Cancelled — nothing deleted.", reply_markup=None, parse_mode="Markdown")
        return
    if action != "ok":
        await query.edit_message_text("Unrecognised button — start over with /delete_account.")
        return

    try:
        await backend.post(f"/api/accounts/{pending['account_id']}/delete")
    except Exception as exc:  # noqa: BLE001
        await query.edit_message_text(f"{original}\n\n❌ Failed — {md(str(exc))}", reply_markup=None, parse_mode="Markdown")
        return

    await query.edit_message_text(
        f"{original}\n\n🗑 Deleted `{md(pending['marzban_username'])}`.",
        reply_markup=None,
        parse_mode="Markdown",
    )
