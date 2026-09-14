"""The customer-facing shop conversation.

Structure worth knowing before editing:

  * There is no admin gate here. Unlike bot/, which is locked to one chat id,
    this bot is meant for the public — so every handler must assume the person
    talking to it is a stranger. Nothing here takes an account id, a username,
    or a price from the message; the backend derives all of those from the
    Telegram user id, which Telegram sets and the sender cannot forge.
  * Per-user flow state lives in context.user_data. It is in-memory and
    deliberately so: it holds only "what did this person tap last", and losing
    it on restart costs a customer one extra tap. Nothing about money is ever
    kept here — the wallet is read from the backend every time it is shown.
"""

from __future__ import annotations

import logging
import re

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from api_client import ShopApiError, backend
import texts

logger = logging.getLogger(__name__)

# Quick-pick volumes. Offered as buttons because most people want a round
# number and typing one is friction; a custom amount is still accepted as
# free text, so this list constrains nothing.
QUICK_VOLUMES = [10, 20, 30, 50, 100]

_STATE = "shop_state"
_PENDING_VOLUME = "pending_volume"
_PENDING_TOPUP = "pending_topup_amount"

# Accepts "35", "35.5", and Persian/Arabic-Indic digits, which phone keyboards
# in Iran produce by default. A customer typing ۳۵ and being told it isn't a
# number is a support message, not a user error.
_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def parse_number(text: str) -> float | None:
    normalised = text.translate(_PERSIAN_DIGITS).strip().replace(",", "").replace("٬", "")
    if not re.fullmatch(r"\d+(?:\.\d+)?", normalised):
        return None
    return float(normalised)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(texts.MENU_BUY), KeyboardButton(texts.MENU_WALLET)],
            [KeyboardButton(texts.MENU_ACCOUNTS), KeyboardButton(texts.MENU_TOPUP)],
            [KeyboardButton(texts.MENU_HELP)],
        ],
        resize_keyboard=True,
    )


async def _session(update: Update) -> dict:
    """Fetches (and lazily creates) this customer's shop record plus the live
    shop config. Called at the top of every flow rather than cached, so a
    price change or the shop closing takes effect on the very next tap instead
    of whenever a cache happened to expire."""
    user = update.effective_user
    return await backend.post("/api/shop/bot/session", json={
        "telegram_id": user.id,
        "telegram_username": user.username,
        "display_name": " ".join(filter(None, [user.first_name, user.last_name])) or None,
    })


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    try:
        await _session(update)
    except ShopApiError:
        logger.exception("Could not open a shop session for %s", update.effective_user.id)
        await update.message.reply_text(texts.GENERIC_ERROR)
        return
    await update.message.reply_text(texts.WELCOME, reply_markup=main_menu())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(texts.HELP, reply_markup=main_menu())


async def show_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(_STATE, None)
    try:
        session = await _session(update)
        history = await backend.get("/api/shop/bot/wallet", params={"telegram_id": update.effective_user.id})
    except ShopApiError:
        logger.exception("Wallet lookup failed for %s", update.effective_user.id)
        await update.message.reply_text(texts.GENERIC_ERROR)
        return

    lines = [texts.wallet_summary(session["balance"])]
    if history:
        lines.append("\nآخرین تراکنش‌ها:")
        for entry in history[:10]:
            sign = "+" if entry["amount"] > 0 else "−"
            lines.append(f"{sign} {abs(entry['amount']):,} — {entry.get('note') or entry['type']}")
    await update.message.reply_text("\n".join(lines), reply_markup=main_menu())


async def show_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(_STATE, None)
    try:
        rows = await backend.get("/api/shop/bot/accounts", params={"telegram_id": update.effective_user.id})
    except ShopApiError:
        logger.exception("Account list failed for %s", update.effective_user.id)
        await update.message.reply_text(texts.GENERIC_ERROR)
        return

    if not rows:
        await update.message.reply_text(texts.NO_ACCOUNTS, reply_markup=main_menu())
        return

    for row in rows:
        used_gb = row["used_traffic"] / (1024 ** 3)
        limit_gb = (row["data_limit"] / (1024 ** 3)) if row["data_limit"] else None
        usage = f"{used_gb:.2f} / {limit_gb:g} گیگ" if limit_gb else f"{used_gb:.2f} گیگ (نامحدود)"
        lines = [f"🔑 {row['marzban_username']}", f"مصرف: {usage}"]
        if row["expire"]:
            from datetime import datetime, timezone
            expires = datetime.fromtimestamp(row["expire"], tz=timezone.utc)
            remaining = (expires - datetime.now(timezone.utc)).days
            lines.append(f"اعتبار: {max(0, remaining)} روز دیگر")
        if row["subscription_url"]:
            # On its own line: a Latin URL inline with Persian text gets
            # reordered by bidirectional rendering and can be copied wrong.
            lines.append("")
            lines.append(row["subscription_url"])
        await update.message.reply_text("\n".join(lines))
    await update.message.reply_text(texts.wallet_summary(
        (await _session(update))["balance"]), reply_markup=main_menu())


# ── buying ────────────────────────────────────────────────────────────────


async def start_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        session = await _session(update)
    except ShopApiError:
        await update.message.reply_text(texts.GENERIC_ERROR)
        return

    if session["is_blocked"]:
        await update.message.reply_text(texts.BLOCKED, reply_markup=main_menu())
        return
    if not session["is_open"]:
        await update.message.reply_text(texts.SHOP_CLOSED, reply_markup=main_menu())
        return

    context.user_data[_STATE] = "awaiting_volume"
    quick = [v for v in QUICK_VOLUMES if session["min_gb"] <= v <= session["max_gb"]]
    keyboard = [[KeyboardButton(f"{v} گیگ") for v in quick[i:i + 3]] for i in range(0, len(quick), 3)]
    keyboard.append([KeyboardButton(texts.MENU_HELP)])
    await update.message.reply_text(
        texts.buy_prompt(session["price_per_gb"], session["min_gb"], session["max_gb"],
                         session["plan_duration_days"]),
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )


async def _handle_volume(update: Update, context: ContextTypes.DEFAULT_TYPE, volume: float) -> None:
    try:
        session = await _session(update)
        quote = await backend.post("/api/shop/bot/quote", json={
            "telegram_id": update.effective_user.id, "data_limit_gb": volume,
        })
    except ShopApiError as exc:
        # A 400 here is the backend's own user-facing sentence (out of range,
        # shop closed) — shown as-is rather than replaced with a generic one.
        await update.message.reply_text(str(exc), reply_markup=main_menu())
        context.user_data.pop(_STATE, None)
        return

    price = quote["price"]
    balance = session["balance"]
    if balance < price:
        short = price - balance
        await update.message.reply_text(
            f"{texts.confirm_purchase(volume, price, quote['duration_days'], balance)}\n\n"
            f"❗️ موجودی شما {texts.money(short)} کم است. اول کیف پولتان را شارژ کنید.",
            reply_markup=main_menu(),
        )
        context.user_data.pop(_STATE, None)
        return

    context.user_data[_STATE] = "confirming_purchase"
    context.user_data[_PENDING_VOLUME] = volume
    await update.message.reply_text(
        texts.confirm_purchase(volume, price, quote["duration_days"], balance),
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("✅ تأیید و خرید")], [KeyboardButton("❌ انصراف")]],
            resize_keyboard=True,
        ),
    )


async def _confirm_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    volume = context.user_data.get(_PENDING_VOLUME)
    # Cleared BEFORE the slow call: a second "تأیید" tap while the first is
    # still running would otherwise buy a second plan the customer never
    # asked for. The backend's own per-user lock is the real guarantee; this
    # stops the duplicate ever being sent.
    context.user_data.pop(_PENDING_VOLUME, None)
    context.user_data.pop(_STATE, None)
    if volume is None:
        await update.message.reply_text(texts.CANCELLED, reply_markup=main_menu())
        return

    await update.message.reply_text(texts.PURCHASE_SENDING, reply_markup=main_menu())
    try:
        result = await backend.post(
            "/api/shop/bot/purchase",
            json={"telegram_id": update.effective_user.id, "data_limit_gb": volume},
            # One Marzban round-trip plus local writes. Generous, because a
            # client-side timeout would NOT cancel the purchase — it would
            # only throw away the reply telling the customer it worked.
            timeout=120,
        )
    except ShopApiError as exc:
        # Covers both "not enough balance" (400) and "couldn't create the
        # account, nothing was charged" (502) — both are sentences written for
        # the customer, including the refunded balance.
        await update.message.reply_text(str(exc), reply_markup=main_menu())
        return

    await update.message.reply_text(
        f"✅ خرید انجام شد.\n\n"
        f"حجم: {texts.gb(result['data_limit_gb'])}\n"
        f"مدت: {result['duration_days']} روز\n"
        f"موجودی باقی‌مانده: {texts.money(result['balance'])}",
        reply_markup=main_menu(),
    )
    try:
        # The QR itself is sent by the BACKEND, which holds the shop bot's
        # token — so the customer gets it even if this process dies between
        # the purchase returning and here.
        await backend.post(f"/api/shop/bot/purchase/{result['order_id']}/deliver")
    except ShopApiError:
        logger.exception("Order %s bought but the QR could not be delivered", result["order_id"])
        if result.get("subscription_url"):
            await update.message.reply_text(
                "لینک اشتراک شما:\n\n" + result["subscription_url"], reply_markup=main_menu()
            )
        else:
            await update.message.reply_text(
                "اشتراک ساخته شد ولی ارسال QR با مشکل مواجه شد. "
                "از «اشتراک‌های من» لینکتان را بردارید.",
                reply_markup=main_menu(),
            )


# ── topping up ────────────────────────────────────────────────────────────


async def start_topup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        session = await _session(update)
    except ShopApiError:
        await update.message.reply_text(texts.GENERIC_ERROR)
        return
    if session["is_blocked"]:
        await update.message.reply_text(texts.BLOCKED, reply_markup=main_menu())
        return
    if not session.get("card_number"):
        # Said plainly instead of walking the customer through entering an
        # amount and only then admitting there is nowhere to send it.
        await update.message.reply_text(texts.TOPUP_NO_CARD, reply_markup=main_menu())
        return

    context.user_data[_STATE] = "awaiting_topup_amount"
    await update.message.reply_text(texts.TOPUP_ASK_AMOUNT, reply_markup=main_menu())


async def _handle_topup_amount(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float) -> None:
    try:
        session = await _session(update)
    except ShopApiError:
        await update.message.reply_text(texts.GENERIC_ERROR)
        return

    amount_int = int(amount)
    if amount_int < session["min_topup"]:
        await update.message.reply_text(f"حداقل مبلغ شارژ {texts.money(session['min_topup'])} است.")
        return
    if amount_int > session["max_topup"]:
        await update.message.reply_text(f"حداکثر مبلغ شارژ {texts.money(session['max_topup'])} است.")
        return

    context.user_data[_STATE] = "awaiting_receipt"
    context.user_data[_PENDING_TOPUP] = amount_int
    await update.message.reply_text(
        texts.topup_instructions(amount_int, session["card_number"], session.get("card_holder")),
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A photo only means anything mid-top-up. Outside that flow it is
    ignored with a hint rather than silently dropped — a customer who sends a
    receipt without pressing the button first would otherwise get no reply at
    all and assume it was received."""
    if context.user_data.get(_STATE) != "awaiting_receipt":
        await update.message.reply_text(
            "برای ثبت رسید، اول «➕ افزایش موجودی» را بزنید و مبلغ را وارد کنید.",
            reply_markup=main_menu(),
        )
        return

    amount = context.user_data.get(_PENDING_TOPUP)
    if amount is None:
        context.user_data.pop(_STATE, None)
        await update.message.reply_text(texts.TOPUP_ASK_AMOUNT, reply_markup=main_menu())
        return

    # The largest rendition Telegram offers. The smallest is often too low-res
    # to read a transaction reference off, which is the whole point of asking.
    file_id = update.message.photo[-1].file_id
    context.user_data.pop(_STATE, None)
    context.user_data.pop(_PENDING_TOPUP, None)

    try:
        await backend.post("/api/shop/bot/topups", json={
            "telegram_id": update.effective_user.id,
            "claimed_amount": amount,
            "receipt_file_id": file_id,
        })
    except ShopApiError as exc:
        await update.message.reply_text(str(exc), reply_markup=main_menu())
        return

    await update.message.reply_text(texts.TOPUP_SUBMITTED, reply_markup=main_menu())


# ── the single text router ────────────────────────────────────────────────


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """One handler for all free text, dispatching on the menu label first and
    the current flow state second.

    Menu labels are checked BEFORE state on purpose: a customer halfway
    through a top-up who taps «اشتراک‌های من» means it, and trapping them in
    the flow until they find a cancel button is exactly the kind of dead end
    that produces support messages.
    """
    text = (update.message.text or "").strip()

    if text == texts.MENU_BUY:
        return await start_buy(update, context)
    if text == texts.MENU_WALLET:
        return await show_wallet(update, context)
    if text == texts.MENU_ACCOUNTS:
        return await show_accounts(update, context)
    if text == texts.MENU_TOPUP:
        return await start_topup(update, context)
    if text == texts.MENU_HELP:
        return await help_command(update, context)
    if text == "❌ انصراف":
        context.user_data.clear()
        return await update.message.reply_text(texts.CANCELLED, reply_markup=main_menu())

    state = context.user_data.get(_STATE)

    if state == "confirming_purchase":
        if text == "✅ تأیید و خرید":
            return await _confirm_purchase(update, context)
        context.user_data.clear()
        return await update.message.reply_text(texts.CANCELLED, reply_markup=main_menu())

    if state == "awaiting_volume":
        # "30 گیگ" from a quick-pick button and a bare "30" typed by hand both
        # arrive here; strip anything that isn't part of the number.
        value = parse_number(text.replace("گیگ", "").replace("GB", "").replace("gb", ""))
        if value is None:
            return await update.message.reply_text(texts.TOPUP_NOT_A_NUMBER)
        return await _handle_volume(update, context, value)

    if state == "awaiting_topup_amount":
        value = parse_number(text)
        if value is None:
            return await update.message.reply_text(texts.TOPUP_NOT_A_NUMBER)
        return await _handle_topup_amount(update, context, value)

    if state == "awaiting_receipt":
        return await update.message.reply_text(texts.TOPUP_NEED_PHOTO, parse_mode=ParseMode.MARKDOWN)

    await update.message.reply_text(texts.WELCOME, reply_markup=main_menu())
