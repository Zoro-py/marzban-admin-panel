from telegram import Update
from telegram.ext import ContextTypes

from api_client import backend
from handlers.common import admin_only, format_expire, format_gb, format_toman, resolve_account


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

    await update.message.reply_text(
        f"Updated `{updated['marzban_username']}` — expires {format_expire(updated['expire'])}, "
        f"limit {format_gb(updated['data_limit'])}{charge_note}",
        parse_mode="Markdown",
    )
