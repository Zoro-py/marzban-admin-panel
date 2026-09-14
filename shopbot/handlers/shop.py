"""The customer-facing shop conversation.

Structure worth knowing before editing:

  * There is no admin gate here. Unlike bot/, which is locked to one chat id,
    this bot is meant for the public — so every handler must assume the person
    talking to it is a stranger. Nothing here takes an account id, a username,
    or a price from the message; the backend derives all of those from the
    Telegram user id, which Telegram sets and the sender cannot forge.

  * ORDER FIRST, THEN PAYMENT. The customer picks a plan while it costs them
    nothing, and the payment request that follows carries one exact figure for
    a thing already chosen. The previous shape made them fund a wallet before
    choosing anything, which meant inventing an amount, doing the price
    multiplication themselves, and — worst — coming BACK after approval to
    place the order they believed they had already placed. Most didn't: the
    money sat in a wallet and the subscription was never collected.

  * NOTHING THE BACKEND SAYS REACHES A CUSTOMER. Its error text is English,
    written for the operator, and for a validation failure it can be a Python
    list. Every sentence here comes from texts.py; anything unmapped falls
    back to texts.generic_error().

  * DELIVERY IS THE BACKEND'S JOB. The QR, the link and the setup guide are
    pushed by backend/app/shop_service.deliver_order_to_customer, because
    delivery fires from two triggers — an instant wallet purchase and an
    operator approving a card payment hours later — and the customer must get
    exactly the same thing either way.

  * Per-user flow state lives in context.user_data. It is in-memory and
    deliberately so: it holds only "what did this person tap last", and losing
    it on restart costs a customer one extra tap. Nothing about money is ever
    kept here — the wallet is read from the backend every time it is shown.
    Menu buttons are checked BEFORE state, so a customer can always leave a
    half-finished flow by tapping the menu rather than being trapped in it.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

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
_STATE_CHOOSING_VOLUME = "choosing_volume"
_STATE_CONFIRMING_WALLET_BUY = "confirming_wallet_buy"
_STATE_CHOOSING_TOPUP = "choosing_topup"
_STATE_AWAITING_RECEIPT = "awaiting_receipt"
_ORDER_ID = "order_id"
_PENDING_AMOUNT = "pending_amount"
_PENDING_VOLUME = "pending_volume"

# A reference code as the backend issues them (4 unambiguous characters, or
# the rare R+6-hex fallback). Typed back into the chat, it returns that
# payment's status — so the code the customer holds actually answers the
# question they are holding it for.
_CODE_PATTERN = re.compile(r"[ACDEFGHJKMNPQRTUVWXYZ2345789]{4}|R[0-9A-F]{6}")

# Wallet history is labelled by what happened, in Persian. It used to print
# the operator's internal English note ("Card payment approved (top-up #12)"),
# which reads to a customer like a glitch.
_WALLET_LABELS = {
    "topup": "شارژ کیف پول",
    "purchase": "خرید سرویس",
    "refund": "بازگشت وجه",
    "adjust": "اصلاح موجودی",
}

# What an Iranian phone keyboard actually produces. Persian (۰-۹) and
# Arabic-Indic (٠-٩) digits both reach us, often mixed with Latin ones.
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
# Separators people type inside numbers. ZWNJ arrives from Persian keyboards;
# the comma variants from pasting out of a bank app.
_SEPARATORS = str.maketrans({c: "" for c in ", ٬،‌_'"})
# Unit words that may trail a number and carry no value: "۲۰ گیگ", "۵۰۰۰۰ تومان".
_TRAILING_UNITS = ("گیگابایت", "گیگابایتی", "گیگ", "گیگی", "تومان", "تومن", "ت", "gb", "GB", "g")
# Multiplier words. These MUST be handled rather than stripped: "۲۰۰ هزار"
# means 200,000, and dropping the word silently yields 200 — a misread, which
# is far worse than a rejection because the customer is never told.
_MULTIPLIERS = (
    ("میلیون", 1_000_000),
    ("ملیون", 1_000_000),
    ("هزار", 1_000),
)


def parse_number(text: str) -> float | None:
    """A number the customer meant, or None.

    Rejects rather than guesses. Every transformation here either preserves
    the value exactly or refuses — a wrong number accepted silently is a
    customer who transfers 200 Toman instead of 200,000 and only finds out
    when their payment is turned down.
    """
    raw = text.translate(_DIGITS).strip()
    if not raw:
        return None

    multiplier = 1
    for word, factor in _MULTIPLIERS:
        if word in raw:
            multiplier = factor
            raw = raw.replace(word, " ")
            break

    lowered = raw.strip()
    for unit in _TRAILING_UNITS:
        if lowered.endswith(unit):
            lowered = lowered[: -len(unit)].strip()
            break

    # Persian decimal separator normalised before thousands separators are
    # stripped, so "۳۵٫۵" survives as 35.5 rather than becoming 355.
    cleaned = lowered.replace("٫", ".").translate(_SEPARATORS).strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", cleaned):
        return None
    return float(cleaned) * multiplier


def main_menu(session: dict | None = None) -> ReplyKeyboardMarkup:
    """The menu adapts to who is looking at it.

    A first-time visitor sees the trial first, and is NOT shown the wallet — a
    stored balance is meaningless to someone who has bought nothing, and
    offering it as a second way to start was the biggest source of confusion
    in the old flow: two buttons that both looked like "begin here", one of
    which was a dead end without the other.

    A returning customer gets the wallet, because for them it is what it was
    always meant to be — a way to skip the card transfer next time.
    """
    trial = bool(session and session.get("trial_available"))
    has_wallet = bool(session and session.get("balance", 0) > 0)
    rows: list[list[KeyboardButton]] = []
    if trial:
        rows.append([KeyboardButton(texts.MENU_TRIAL)])
    rows.append([KeyboardButton(texts.MENU_BUY), KeyboardButton(texts.MENU_ACCOUNTS)])
    if has_wallet or (session is not None and not trial):
        rows.append([KeyboardButton(texts.MENU_WALLET), KeyboardButton(texts.MENU_TOPUP)])
    rows.append([KeyboardButton(texts.MENU_HELP), KeyboardButton(texts.MENU_SUPPORT)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _handle(session: dict | None) -> str | None:
    return (session or {}).get("support_handle")


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


async def _reply(update: Update, text: str, session: dict | None = None, **kwargs) -> None:
    """Every reply carries a keyboard.

    A message sent without one strands the customer on whatever keyboard was
    last shown — which, mid-purchase, is a row of volume buttons with no way
    back to the menu. Centralised so a new handler cannot forget.
    """
    await update.message.reply_text(text, reply_markup=main_menu(session), **kwargs)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    try:
        session = await _session(update)
    except ShopApiError:
        logger.exception("Could not open a shop session for %s", update.effective_user.id)
        await update.message.reply_text(texts.generic_error(None))
        return
    await _reply(
        update,
        texts.welcome(
            session.get("shop_name"),
            session.get("trial_available", False),
            session.get("trial_gb", 0),
            session.get("trial_hours", 0),
        ),
        session,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = await _session(update)
    await _reply(
        update,
        texts.help_text(
            session["price_per_gb"], session["plan_duration_days"],
            session.get("approval_eta_minutes", 30), _handle(session),
        ),
        session,
    )


async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = await _session(update)
    await _reply(update, texts.support_text(_handle(session)), session)


# ── the trial ─────────────────────────────────────────────────────────────


async def take_trial(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The shop going first.

    Delivery is pushed by the BACKEND, not sent from here — so a trial arrives
    looking exactly like a purchase, and the most important message in the
    product exists in one place.
    """
    context.user_data.clear()
    session = await _session(update)
    if not session.get("trial_enabled"):
        await _reply(update, texts.TRIAL_UNAVAILABLE, session)
        return
    if not session.get("trial_available"):
        await _reply(update, texts.TRIAL_ALREADY_TAKEN, session)
        return

    await _reply(update, texts.BUY_WORKING, session)
    user = update.effective_user
    try:
        await backend.post("/api/shop/bot/trial", json={
            "telegram_id": user.id,
            "telegram_username": user.username,
            "display_name": " ".join(filter(None, [user.first_name, user.last_name])) or None,
        }, timeout=90)
    except ShopApiError:
        logger.exception("Trial failed for %s", user.id)
        await _reply(update, texts.generic_error(_handle(session)), session)
        return
    # No success message: the backend's delivery lands within a second and
    # says it better. A "done!" from the bot first would just be noise above
    # the thing the customer actually wants.


# ── buying, order first ───────────────────────────────────────────────────


async def start_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    session = await _session(update)
    if session["is_blocked"]:
        await _reply(update, texts.blocked(_handle(session)), session)
        return
    if not session["is_open"]:
        await _reply(update, texts.shop_closed(_handle(session)), session)
        return

    context.user_data[_STATE] = _STATE_CHOOSING_VOLUME
    quick = [v for v in QUICK_VOLUMES if session["min_gb"] <= v <= session["max_gb"]]
    keyboard = [[KeyboardButton(texts.gb(v)) for v in quick[i:i + 3]] for i in range(0, len(quick), 3)]
    keyboard.append([KeyboardButton(texts.CANCEL)])
    await update.message.reply_text(
        texts.buy_prompt(
            session["price_per_gb"], session["min_gb"], session["max_gb"],
            session["plan_duration_days"],
        ),
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )


def _payment_request_text(intent: dict) -> str:
    """The plan, then the one number to transfer.

    When the wallet already holds something, that part is stated separately so
    the figure being asked for is unambiguous — the customer types the
    shortfall into their bank app, not the price.
    """
    head = f"🛒 {texts.gb(intent['data_limit_gb'])} — {texts.fa(intent['duration_days'])} روزه\n"
    if intent["balance"] > 0:
        head += (
            f"قیمت: {texts.money(intent['price'])}\n"
            f"از کیف پولتان کم می‌شود: {texts.money(intent['balance'])}\n"
        )
    return head + "\n" + texts.topup_instructions(
        intent["shortfall"], intent["card_number"], intent.get("card_holder"),
        intent.get("approval_eta_minutes", 30),
    )


async def _handle_volume(update: Update, context: ContextTypes.DEFAULT_TYPE, volume: float) -> None:
    """Volume chosen. Creates the ORDER — which takes no money — then shows
    whichever of the two payment screens applies."""
    session = await _session(update)
    if not (session["min_gb"] <= volume <= session["max_gb"]):
        await update.message.reply_text(texts.out_of_range(session["min_gb"], session["max_gb"]))
        return

    try:
        intent = await backend.post("/api/shop/bot/orders", json={
            "telegram_id": update.effective_user.id,
            "data_limit_gb": volume,
        })
    except ShopApiError:
        logger.exception("Could not create an order for %s", update.effective_user.id)
        context.user_data.clear()
        await _reply(update, texts.generic_error(_handle(session)), session)
        return

    context.user_data[_ORDER_ID] = intent["order_id"]
    context.user_data[_PENDING_VOLUME] = intent["data_limit_gb"]

    if intent["payable_from_wallet"]:
        # Enough credit already: one tap, no card, no human in the loop.
        context.user_data[_STATE] = _STATE_CONFIRMING_WALLET_BUY
        await update.message.reply_text(
            texts.confirm_from_wallet(
                intent["data_limit_gb"], intent["duration_days"],
                intent["price"], intent["balance"],
            ),
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton(texts.CONFIRM_BUY)], [KeyboardButton(texts.CANCEL)]],
                resize_keyboard=True,
            ),
        )
        return

    if not intent.get("card_number"):
        context.user_data.clear()
        await _reply(update, texts.NO_CARD, session)
        return

    context.user_data[_STATE] = _STATE_AWAITING_RECEIPT
    context.user_data[_PENDING_AMOUNT] = intent["shortfall"]
    await update.message.reply_text(
        _payment_request_text(intent),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton(texts.CANCEL)]], resize_keyboard=True),
    )


async def _confirm_wallet_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    order_id = context.user_data.get(_ORDER_ID)
    session = await _session(update)
    if not order_id:
        context.user_data.clear()
        await _reply(update, texts.not_understood(_handle(session)), session)
        return

    # Cleared BEFORE the slow call, so a second tap during the few seconds
    # provisioning takes cannot start a second purchase.
    context.user_data.clear()
    await _reply(update, texts.BUY_WORKING, session)
    try:
        await backend.post(f"/api/shop/bot/orders/{order_id}/pay", timeout=120)
    except ShopApiError:
        logger.exception("Wallet purchase failed for order %s", order_id)
        await _reply(update, texts.generic_error(_handle(session)), session)
        return
    # Delivery is pushed by the backend — the same path an approved card
    # payment takes, so the customer receives an identical message either way.


# ── topping up a wallet directly (repeat customers) ───────────────────────


async def start_topup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    session = await _session(update)
    if session["is_blocked"]:
        await _reply(update, texts.blocked(_handle(session)), session)
        return
    if not session.get("card_number"):
        await _reply(update, texts.NO_CARD, session)
        return
    context.user_data[_STATE] = _STATE_CHOOSING_TOPUP
    await update.message.reply_text(
        texts.topup_ask_amount(session["min_topup"], session["max_topup"]),
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton(texts.CANCEL)]], resize_keyboard=True),
    )


async def _handle_topup_amount(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float) -> None:
    session = await _session(update)
    value = int(amount)
    if not (session["min_topup"] <= value <= session["max_topup"]):
        await update.message.reply_text(
            texts.topup_out_of_range(session["min_topup"], session["max_topup"])
        )
        return
    context.user_data[_STATE] = _STATE_AWAITING_RECEIPT
    context.user_data[_PENDING_AMOUNT] = value
    # No order attached: this is a plain wallet top-up.
    context.user_data.pop(_ORDER_ID, None)
    await update.message.reply_text(
        texts.topup_instructions(
            value, session["card_number"], session.get("card_holder"),
            session.get("approval_eta_minutes", 30),
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton(texts.CANCEL)]], resize_keyboard=True),
    )


# ── receipts ──────────────────────────────────────────────────────────────


def _topup_submitted_text(reference, eta: int, handle, has_order: bool,
                          volume_gb: float | None = None) -> str:
    """Read at the highest-anxiety moment in the product: the customer has just
    sent real money to a stranger's card and now holds nothing.

    Each line answers a question they actually have — is it received, how
    long, what do I hold, what if nothing happens — in that order. The
    reference code appears here rather than on the payment screen because this
    is where it becomes useful: before paying they hold the card number and
    the amount; after paying they hold only the waiting.
    """
    # ONE time promise. An earlier version added "if nothing in 120 minutes,
    # send this code" under a line promising 30 — two deadlines in one breath,
    # the second reading as a stall. The late case is now handled by a push
    # the moment the promise is missed (shop_service.notify_overdue_payments),
    # so this message only has to make the promise, not hedge it.
    what = f" — برای سرویس {texts.gb(volume_gb)}" if (has_order and volume_gb) else ""
    lines = [f"✅ رسیدتان رسید{what}."]
    if has_order:
        lines.append(f"تا {texts.fa(eta)} دقیقه بررسی می‌شود و سرویس‌تان خودکار آماده می‌شود.")
    else:
        lines.append(f"تا {texts.fa(eta)} دقیقه بررسی می‌شود و کیف پولتان شارژ می‌شود.")
    lines.append("لازم نیست ربات را باز نگه دارید — خبرش همینجا می‌آید.")
    if reference:
        # Says what the code is FOR. A code with no use reads as decoration;
        # this one returns the payment's status the moment it is typed back.
        lines.append(f"کد پیگیری: {reference} — هر وقت خواستید همین کد را اینجا بفرستید تا وضعیتش را ببینید.")
    return "\n".join(lines) + texts.support(handle)


async def _recover_pending_order(update: Update) -> dict | None:
    """The plan this receipt belongs to, when the bot has lost the thread.

    The usual reason in Iran is not a restart: banking apps refuse to open
    over a VPN, so the customer turns the VPN off to pay, Telegram drops with
    it, and the receipt arrives in what looks like a fresh conversation.
    Asking the backend for their waiting order means the receipt still lands
    on the right plan instead of being bounced as "what is this for?".
    """
    try:
        pending = await backend.get(
            "/api/shop/bot/orders/pending", params={"telegram_id": update.effective_user.id},
        )
    except ShopApiError:
        logger.exception("Could not look up a pending order for %s", update.effective_user.id)
        return None
    if pending and pending.get("shortfall", 0) > 0:
        return pending
    return None


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = await _session(update)
    amount = context.user_data.get(_PENDING_AMOUNT)
    order_id = context.user_data.get(_ORDER_ID)
    volume = context.user_data.get(_PENDING_VOLUME)

    if context.user_data.get(_STATE) != _STATE_AWAITING_RECEIPT or not amount:
        recovered = await _recover_pending_order(update)
        if recovered is None:
            # Genuinely nothing waiting. Says what it needs rather than
            # silently ignoring the photo — an ignored receipt is a customer
            # who believes they have paid, waiting for a service nobody is
            # making.
            await _reply(update, texts.RECEIPT_WITHOUT_CONTEXT, session)
            return
        amount = recovered["shortfall"]
        order_id = recovered["order_id"]
        volume = recovered["data_limit_gb"]

    # Cleared before the call: a second photo sent while this one is in flight
    # must not open a second payment against the same order.
    context.user_data.clear()

    try:
        topup = await backend.post("/api/shop/bot/topups", json={
            "telegram_id": update.effective_user.id,
            "claimed_amount": amount,
            "receipt_file_id": update.message.photo[-1].file_id,
            "order_id": order_id,
        }, timeout=60)
    except ShopApiError:
        logger.exception("Could not record a receipt for %s", update.effective_user.id)
        # Specific, not the generic error: the customer's question here is
        # "did I just lose my money?", and the true answer is no — nothing was
        # recorded, so resending the same photo is safe.
        await _reply(update, texts.receipt_failed(_handle(session)), session)
        return

    await _reply(
        update,
        _topup_submitted_text(
            topup.get("reference_code"),
            session.get("approval_eta_minutes", 30),
            _handle(session),
            has_order=order_id is not None,
            volume_gb=volume,
        ),
        session,
    )


# ── wallet & accounts ─────────────────────────────────────────────────────


async def show_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    try:
        session = await _session(update)
        history = await backend.get("/api/shop/bot/wallet", params={"telegram_id": update.effective_user.id})
    except ShopApiError:
        logger.exception("Wallet lookup failed for %s", update.effective_user.id)
        await update.message.reply_text(texts.generic_error(None))
        return

    if not session["balance"] and not history:
        await _reply(update, texts.WALLET_EMPTY, session)
        return

    lines = [texts.wallet_summary(session["balance"])]
    if history:
        lines.append("\nآخرین تراکنش‌ها:")
        for entry in history[:10]:
            # Rendered as a sentence rather than a signed number. A leading
            # "+"/"−" is a bidi-neutral character: in right-to-left text it
            # detaches from its digits and can render on the wrong side, which
            # on a money line is a support message. The English enum used as a
            # fallback label leaked through here too.
            label = _WALLET_LABELS.get(entry.get("type"), "تراکنش")
            lines.append(f"{label}: {texts.money(abs(entry['amount']))}")
    await _reply(update, "\n".join(lines), session)


async def show_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    try:
        session = await _session(update)
        rows = await backend.get("/api/shop/bot/accounts", params={"telegram_id": update.effective_user.id})
    except ShopApiError:
        logger.exception("Account list failed for %s", update.effective_user.id)
        await update.message.reply_text(texts.generic_error(None))
        return

    if not rows:
        await _reply(update, texts.NO_ACCOUNTS, session)
        return

    for row in rows:
        used_gb = row["used_traffic"] / (1024 ** 3)
        limit_gb = (row["data_limit"] / (1024 ** 3)) if row["data_limit"] else None
        days_left = None
        if row["expire"]:
            expires = datetime.fromtimestamp(row["expire"], tz=timezone.utc)
            days_left = (expires - datetime.now(timezone.utc)).days
        lines = [texts.account_line(limit_gb, used_gb, days_left)]
        if row["subscription_url"]:
            # On its own line: a Latin URL inline with Persian text gets
            # reordered by bidirectional rendering and can be copied wrong.
            lines.append("")
            lines.append(row["subscription_url"])
        await update.message.reply_text("\n".join(lines))

    await _reply(update, texts.wallet_summary(session["balance"]), session)


# ── routing ───────────────────────────────────────────────────────────────


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menu buttons are matched BEFORE any in-flight state.

    That ordering is the escape hatch: a customer halfway through a top-up who
    taps "🛒 خرید سرویس" gets the buy screen, not a complaint that their
    message isn't a number. Being unable to leave a flow except by /start was
    a real dead end in the previous version.
    """
    text = (update.message.text or "").strip()

    if text == texts.MENU_BUY:
        return await start_buy(update, context)
    if text == texts.MENU_TRIAL:
        return await take_trial(update, context)
    if text == texts.MENU_ACCOUNTS:
        return await show_accounts(update, context)
    if text == texts.MENU_WALLET:
        return await show_wallet(update, context)
    if text == texts.MENU_TOPUP:
        return await start_topup(update, context)
    if text == texts.MENU_HELP:
        return await help_command(update, context)
    if text == texts.MENU_SUPPORT:
        return await show_support(update, context)

    if text == texts.CANCEL:
        # Only claim a cancellation when something was actually in progress.
        # Updates are processed in order, so a Cancel tapped during a purchase
        # runs AFTER it — and answering "cancelled" then told a customer whose
        # service was being delivered that it had been stopped.
        had_flow = context.user_data.get(_STATE) is not None
        context.user_data.clear()
        session = await _session(update)
        await _reply(update, texts.CANCELLED if had_flow else texts.NOTHING_TO_CANCEL, session)
        return

    state = context.user_data.get(_STATE)

    if state == _STATE_CONFIRMING_WALLET_BUY and text == texts.CONFIRM_BUY:
        return await _confirm_wallet_purchase(update, context)

    if state == _STATE_CHOOSING_VOLUME:
        volume = parse_number(text)
        if volume is None:
            await update.message.reply_text(texts.not_a_number("۳۵"))
            return
        return await _handle_volume(update, context, volume)

    if state == _STATE_CHOOSING_TOPUP:
        amount = parse_number(text)
        if amount is None:
            await update.message.reply_text(texts.not_a_number("۲۰۰,۰۰۰"))
            return
        return await _handle_topup_amount(update, context, amount)

    if state == _STATE_AWAITING_RECEIPT:
        # They are expected to send a photo. Tell them that instead of
        # dropping the message — someone typing "واریز کردم" here is telling
        # us something and deserves an answer.
        await update.message.reply_text(texts.NEED_PHOTO)
        return

    session = await _session(update)

    code = text.upper()
    if _CODE_PATTERN.fullmatch(code):
        try:
            info = await backend.get(
                "/api/shop/bot/topups/status",
                params={"telegram_id": update.effective_user.id, "code": code},
            )
        except ShopApiError:
            await _reply(update, texts.CODE_NOT_FOUND, session)
            return
        await _reply(
            update,
            texts.payment_status(
                info["status"], info["reference_code"], info.get("order_status"),
                info.get("data_limit_gb"), info.get("reject_reason"), _handle(session),
            ),
            session,
        )
        return

    await _reply(update, texts.not_understood(_handle(session)), session)
