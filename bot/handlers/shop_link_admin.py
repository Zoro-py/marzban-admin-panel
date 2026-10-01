"""Operator commands to link EXISTING customers to the shop bot — the shop
counterpart of handlers/delegate_admin.py's /delegate_invite pair, with the
same shape and the same reasoning behind it.

/shoplink mints a one-time t.me deep link (POST /api/shop/link-invite) that
binds the CUSTOMER to the Telegram identity that taps it, so their
operator-created accounts show up in shopbot's «سرویس‌های من» and a normal
purchase renews their real account in place, paid from the shop wallet. No
money moves at invite time — this binds identities, never balances.

/shoplink needs no Confirm step, exactly like /delegate_invite: it creates
only a PENDING invite that grants nothing until the customer taps the link,
so a misresolve is reversible with /shoplink_off and the backend's own
eligibility gates (payg billing, group representative, already linked, an
invite already pending) surface as readable 409 details on the same error
path.

/shoplink_off is the operator's off-switch (DELETE /api/shop/link/{id}): it
clears the customer-side link and any pending invite with it, leaving the
ShopUser, its wallet and every account row untouched.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from api_client import backend
from handlers.common import AmbiguousMatch, admin_only, md, resolve_customer


async def _reply_ambiguous(update: Update, matches: list[dict]) -> None:
    lines = ["Multiple customers match — retry with the numeric id:"]
    for c in matches:
        lines.append(f"• #{c['id']} — {c['name']}")
    await update.message.reply_text("\n".join(lines))


@admin_only
async def shoplink_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 1:
        await update.message.reply_text(
            "Usage: `/shoplink <customer name or id>`\n"
            "Creates a one-time t.me deep link for that EXISTING customer — "
            "they tap it and their operator-created accounts become visible "
            "in shopbot («سرویس‌های من»), where a purchase renews their real "
            "account from the shop wallet. The link is single-use and "
            "expires in 7 days; discard an unused one with "
            "/shoplink_off <customer>. Refused for pay-as-you-go customers "
            "and group representatives.",
            parse_mode="Markdown",
        )
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

    try:
        invite = await backend.post("/api/shop/link-invite", json={"customer_id": customer["id"]})
    except Exception as exc:  # noqa: BLE001 — 409 (payg / group rep / already linked / pending exists) and 503 (username unset) land here too
        await update.message.reply_text(f"Could not create the invite: {exc}")
        return

    # Message 1 is the forward-ready block — the operator forwards THIS
    # message (and only this one) to the customer, so the operator-facing
    # note deliberately goes in a separate message (same split as
    # /delegate_invite: a careless forward-everything can't leak it).
    forward_block = (
        f"سلام {invite['customer_name']} عزیز!\n"
        "این لینک اختصاصی شماست — با زدن آن، سرویس‌های فعلی‌تان به ربات فروش وصل می‌شود: "
        "وضعیت سرویس‌ها را می‌بینید، کیف پولتان را شارژ می‌کنید و همان سرویس قبلی را تمدید می‌کنید "
        "(حجم تازه به سرویس فعلی اضافه می‌شود و لینکی که در برنامه دارید کار می‌کند).\n"
        "لینک فقط یک‌بار قابل استفاده است و ۷ روز اعتبار دارد:\n"
        "\n"
        f"{invite['invite_url']}"
    )
    await update.message.reply_text(forward_block)
    await update.message.reply_text(
        f"✅ Pending shop link for {md(invite['customer_name'])} "
        f"(customer id {invite['customer_id']}) created.\n"
        "Single-use: the first Telegram account that taps it claims it — "
        "refused at claim time if that Telegram is already bound to another "
        "customer.\n"
        f"Discard if unused: /shoplink_off {invite['customer_id']}\n"
        "The same card lives on the customer's dashboard page (Shop bot section).",
        parse_mode="Markdown",
    )


@admin_only
async def shoplink_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 1:
        await update.message.reply_text(
            "Usage: `/shoplink_off <customer name or id>`\n"
            "Unlinks that customer from the shop bot (killing a pending "
            "invite too, if one is live). Their shop wallet, orders and "
            "accounts are untouched — they only lose shop-bot access.",
            parse_mode="Markdown",
        )
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

    try:
        await backend.delete(f"/api/shop/link/{customer['id']}")
    except Exception as exc:  # noqa: BLE001 — 404 (not linked, nothing pending) lands here too
        await update.message.reply_text(f"Could not unlink {md(customer['name'])}: {exc}")
        return
    await update.message.reply_text(
        f"⛔ Shop-bot link removed for {md(customer['name'])} (id {customer['id']}).",
        parse_mode="Markdown",
    )
