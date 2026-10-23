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

# F13 (2026-10-23): /extend joins the same pending-confirm pattern — it moves
# real money (the GB charge posts to the ledger before the adjust), which is
# exactly the class of command those two never run bare. Each flow keeps its
# own counter and callback prefix ("extend:" vs "delacc:") so tokens can
# never collide across features.
_pending_extend: dict[int, dict] = {}
_next_extend_token = itertools.count(1)


@admin_only
async def extend_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/extend <username> <days>` (negative days to reduce), "
            "or `/extend <username> <days> <gb>` to also adjust the data limit and bill it "
            "at this account's rate (same as the dashboard's Adjust section — days alone are never billed). "
            "Asks for confirmation before it changes anything.",
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

    # This command used to apply the Marzban change and stop — no ledger
    # entry, ever, for any amount of data added. Mirrors the dashboard's own
    # Adjust section instead: GB added is billed at this account's effective
    # rate by default (days alone are never billed — price is per GB, not per
    # day). account is the pre-adjust AccountRow from resolve_account, which
    # already carries customer_id/group_id/effective_rate/rate_configured —
    # none of that changes from an adjust.
    #
    # F13: nothing below POSTS anything. It builds the charge PLAN (and the
    # plain-language reason when it is not billed), shows the operator a
    # Confirm/Cancel card — the same popped-token shape as /wallet and
    # /delete_account — and extend_callback does the posts on Confirm. The
    # MONEY-ADJ-1 ordering lives there now, unchanged: charge FIRST, then the
    # adjust carrying bill_added_gb=True so the backend marks the added GB
    # billed in the same transaction as the size change. The old order
    # (adjust, then charge) left the added GB showing as pending too, so the
    # next settle billed it a second time. If the adjust fails after the
    # charge landed, the money is VISIBLE in the ledger — the operator
    # credits it back or retries the adjust (the safe direction; the reverse
    # would hide it).
    ledger_body: dict | None = None
    if gb is not None and gb > 0:
        if account.get("customer_id") is None and account.get("group_id") is None:
            card_note = "Not billed — unassigned account."
        elif not account.get("rate_configured"):
            card_note = "Not billed — no rate configured."
        else:
            amount = round(gb * account["effective_rate"], 2)
            if amount > 0:
                ledger_body = {
                    "type": "charge",
                    "amount": amount,
                    "customer_id": account.get("customer_id"),
                    "group_id": account.get("group_id"),
                    "account_id": account["id"],
                    "note": f"+{gb}GB via bot /extend",
                }
                card_note = f"Will charge {format_toman(amount)} to the ledger FIRST, then apply the change."
            else:
                card_note = "Not billed — the computed charge rounds to zero."
    else:
        card_note = "No GB charge — days alone are never billed."

    token = next(_next_extend_token)
    _pending_extend[token] = {
        "account_id": account["id"],
        "marzban_username": account["marzban_username"],
        "body": body,
        "ledger_body": ledger_body,
        # Ties both POSTs to the exact Telegram update that started this flow
        # (same reasoning as customer.py's CLAIM 7 keys): a redelivered
        # callback or a retry of this same confirmation hits the backend's
        # Idempotency-Key handling instead of writing a second charge.
        "update_id": update.update_id,
    }

    deltas = f"{days:+d} day(s)"
    if gb is not None:
        deltas += f", {gb:+g} GB"
    text = (
        f"⚠️ Update `{md(account['marzban_username'])}`: {deltas}\n"
        f"{card_note}\n\nConfirm?"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data=f"extend:ok:{token}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"extend:no:{token}"),
    ]])
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


@admin_only
async def extend_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The Confirm/Cancel half of /extend — all posting happens here, exactly
    once, because the pending token is POPPED before anything is sent (a
    double-tap or a Telegram redelivery cannot replay the adjustment; same
    shape as wallet_callback). The Idempotency-Key on each POST is derived
    from the /extend update that opened this confirmation, so a retry of this
    same logical action is answered by the backend's first result instead of
    writing a second charge."""
    query = update.callback_query
    await query.answer()
    try:
        _, action, raw_token = query.data.split(":", 2)
        token = int(raw_token)
    except (ValueError, AttributeError):
        await query.edit_message_text("This button is malformed — start over with /extend.")
        return

    pending = _pending_extend.pop(token, None)
    if pending is None:
        await query.edit_message_text("This confirmation already expired or was used — start over with /extend.")
        return

    # The confirmation message was sent with parse_mode=Markdown and an edit
    # does not inherit that — it must be resupplied (see wallet_callback).
    original = query.message.text or ""
    if action == "no":
        await query.edit_message_text(f"{original}\n\n❌ Cancelled — nothing changed.", reply_markup=None, parse_mode="Markdown")
        return
    if action != "ok":
        await query.edit_message_text("Unrecognised button — start over with /extend.")
        return

    # MONEY-ADJ-1: the charge goes FIRST, then the adjust carries
    # bill_added_gb=True (see extend_command's comment for the full why).
    charge_note = ""
    bill_added_gb = False
    ledger_body = pending["ledger_body"]
    if ledger_body is not None:
        try:
            await backend.post(
                "/api/ledger",
                json=ledger_body,
                # The ledger endpoint and the adjust endpoint each keep their
                # own idempotency store, but the two keys stay distinguishable
                # anyway ("tg-extend-<id>" vs "tg-extend-adjust-<id>") so the
                # pair can never be mistaken for one replayed request.
                idempotency_key=f"tg-extend-{pending['update_id']}",
            )
            charge_note = f" — charged {format_toman(ledger_body['amount'])}"
            bill_added_gb = True
        except Exception as exc:  # noqa: BLE001
            # The charge did NOT go through; do not pass
            # bill_added_gb (the added GB stays pending and stays
            # visible for the next settle instead of vanishing).
            charge_note = (f" — ⚠️ NOT billed ({exc}). "
                           f"Charge {format_toman(ledger_body['amount'])} by hand or the next settle will.")

    body = dict(pending["body"])
    if bill_added_gb:
        body["bill_added_gb"] = True

    try:
        updated = await backend.post(
            f"/api/accounts/{pending['account_id']}/adjust",
            json=body,
            idempotency_key=f"tg-extend-adjust-{pending['update_id']}",
        )
    except ValueError as exc:
        if bill_added_gb:
            # The GB charge landed but the panel change did not — the ledger
            # row is visible; the operator must credit it back or retry.
            await query.edit_message_text(
                f"{original}\n\n⚠️ The GB charge was recorded, but the adjust FAILED ({md(str(exc))}). "
                "Credit the charge back by hand, or retry /extend.",
                reply_markup=None,
                parse_mode="Markdown",
            )
            return
        await query.edit_message_text(
            f"{original}\n\n❌ Failed — {md(str(exc))}", reply_markup=None, parse_mode="Markdown",
        )
        return

    await query.edit_message_text(
        f"{original}\n\n✅ Updated `{md(pending['marzban_username'])}` — expires {format_expire(updated['expire'])}, "
        f"limit {format_gb(updated['data_limit'])}{charge_note}",
        reply_markup=None,
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
