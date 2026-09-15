"""Operator commands to grant/list/revoke delegated self-service access —
see backend/app/models.py's Delegate docstring for what the grant actually
allows. /delegate_add uses the SAME fuzzy resolve_customer() as /charge and
/customer: safe here (unlike wallet.py's exact-id-only rule) because this
only names WHICH of the operator's own known customers gets a scoped grant
— it never moves money and never targets a stranger."""

from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from api_client import backend
from handlers.common import AmbiguousMatch, admin_only, format_toman, md, resolve_customer


async def _reply_ambiguous(update: Update, matches: list[dict]) -> None:
    lines = ["Multiple customers match — retry with the numeric id:"]
    for c in matches:
        lines.append(f"• #{c['id']} — {c['name']}")
    await update.message.reply_text("\n".join(lines))


@admin_only
async def delegate_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # telegram_id FIRST and always numeric — unlike /charge's <name> <amount>,
    # a customer name can itself be all-digits (a phone number, say), so
    # putting the unambiguous numeric token first avoids ever having to
    # guess which trailing token is which.
    if len(context.args) < 2 or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text(
            "Usage: `/delegate_add <telegram_id> <customer name or id> [credit_limit]`\n"
            "Grants that customer's own Telegram account direct self-service "
            "(create/renew/delete their OWN accounts) via delegate_bot — never "
            "any visibility into their balance or the ledger.\n"
            "Re-run with the same telegram_id to edit an existing grant.",
            parse_mode="Markdown",
        )
        return
    telegram_id = int(context.args[0])

    rest = context.args[1:]
    # The trailing token is credit_limit ONLY if it parses as a number AND
    # there's still a name left over once it's taken off — a non-numeric
    # last token, or a numeric one that IS the whole name, just stays part
    # of the customer name instead of erroring.
    credit_limit: Optional[float] = None
    if len(rest) >= 2:
        try:
            credit_limit = float(rest[-1].replace(",", ""))
            rest = rest[:-1]
        except ValueError:
            pass

    query = " ".join(rest)
    if not query:
        await update.message.reply_text("Usage: /delegate_add <telegram_id> <customer name or id> [credit_limit]")
        return

    try:
        customer = await resolve_customer(query)
    except AmbiguousMatch as exc:
        await _reply_ambiguous(update, exc.matches)
        return
    if customer is None:
        await update.message.reply_text(f"No customer matches '{query}'.")
        return

    try:
        delegate = await backend.post("/api/delegate", json={
            "customer_id": customer["id"],
            "telegram_id": telegram_id,
            "credit_limit": credit_limit,
        })
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"Could not save the grant: {exc}")
        return

    cap_line = f"credit limit {format_toman(credit_limit)}" if credit_limit is not None else "no credit limit (unlimited trust)"
    await update.message.reply_text(
        f"✅ Delegate access granted: *{md(customer['name'])}* (id `{telegram_id}`), {cap_line}.\n"
        "They can now message delegate_bot to create/renew/delete their own accounts.",
        parse_mode="Markdown",
    )


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
    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /delegate_off <telegram_id>")
        return
    telegram_id = int(context.args[0])
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
