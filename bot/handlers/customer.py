from telegram import Update
from telegram.ext import ContextTypes

from api_client import backend
from handlers.common import AmbiguousMatch, admin_only, format_gb, format_toman, resolve_customer


async def _reply_ambiguous(update: Update, matches: list[dict]) -> None:
    lines = ["Multiple customers match — retry with the numeric id:"]
    for c in matches:
        lines.append(f"• #{c['id']} — {c['name']}")
    await update.message.reply_text("\n".join(lines))


@admin_only
async def customer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /customer <name or id>")
        return

    query = " ".join(context.args)
    try:
        customer = await resolve_customer(query)
    except AmbiguousMatch as exc:
        await _reply_ambiguous(update, exc.matches)
        return

    if customer is None:
        await update.message.reply_text(f"No customer matches '{query}'.")
        return

    balance = await backend.get("/api/ledger/balance", params={"customer_id": customer["id"]})
    accounts = await backend.get(f"/api/customers/{customer['id']}/accounts")

    lines = [
        f"*{customer['name']}*  (#{customer['id']})",
        f"Contact: {customer.get('contact') or '—'}",
        f"Balance: {format_toman(balance['balance'])} ({'owed' if balance['balance'] > 0 else 'credit' if balance['balance'] < 0 else 'settled'})",
        "",
        f"*Accounts ({len(accounts)})*",
    ]
    for a in accounts:
        lines.append(f"• `{a['marzban_username']}` — used {format_gb(a['used_traffic'])} / {format_gb(a['data_limit'])}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _ledger_command(update: Update, context: ContextTypes.DEFAULT_TYPE, ledger_type: str) -> None:
    if len(context.args) < 2:
        await update.message.reply_text(f"Usage: /{ledger_type} <customer name or id> <amount> [note...]")
        return

    # Customer query must be a single token (name substring or id) so `amount` is
    # unambiguously the second argument; anything after that is a free-text note.
    query = context.args[0]
    amount_str = context.args[1]
    note = " ".join(context.args[2:]) if len(context.args) > 2 else None

    try:
        amount = float(amount_str)
    except ValueError:
        await update.message.reply_text("Amount must be a number, e.g. `/charge boojar 150000 monthly renewal`", parse_mode="Markdown")
        return

    try:
        customer = await resolve_customer(query)
    except AmbiguousMatch as exc:
        await _reply_ambiguous(update, exc.matches)
        return

    if customer is None:
        await update.message.reply_text(f"No customer matches '{query}'.")
        return

    entry = await backend.post(
        "/api/ledger",
        json={"type": ledger_type, "amount": amount, "customer_id": customer["id"], "note": note},
    )
    label = "Debt" if ledger_type == "charge" else "Credit"
    await update.message.reply_text(f"{label} recorded for *{customer['name']}*: {format_toman(entry['amount'])}", parse_mode="Markdown")


@admin_only
async def charge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ledger_command(update, context, "charge")


@admin_only
async def credit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ledger_command(update, context, "credit")
