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
# Per-process, not persisted: worst case a bot restart offers the phone
# prompt one more time than intended, which is a fully dismissible, harmless
# repeat, not a reason to migrate a new column for it.
_PHONE_ASKED = "phone_asked"

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

    lowered = raw.strip().lower()
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


async def _maybe_offer_phone_share(update: Update, context: ContextTypes.DEFAULT_TYPE, session: dict) -> None:
    """Offered exactly once, right after a first real purchase lands — never
    on /start (a stranger who hasn't seen a price yet asked for their phone
    number reads as a scam signal and costs conversions), and never
    required for anything downstream. See PHONE_LATER/handle_contact for
    the other two paths out of this prompt.

    Tracked in chat_data, NOT user_data: almost every handler in this file
    clears user_data at its start (it holds "what did this person tap
    last", meant to reset on every new flow — see the module docstring), so
    a flag stored there would be wiped before the very next message and
    "ask once" would silently become "ask every time"."""
    if session.get("phone") or context.chat_data.get(_PHONE_ASKED):
        return
    context.chat_data[_PHONE_ASKED] = True
    await update.effective_message.reply_text(
        texts.SHARE_PHONE_PROMPT,
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton(texts.SHARE_PHONE_BUTTON, request_contact=True)], [KeyboardButton(texts.PHONE_LATER)]],
            resize_keyboard=True,
        ),
    )


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    contact = update.message.contact
    session = await _session(update)
    # Only the customer's OWN number — accepting one shared on someone
    # else's behalf would attribute a stranger's phone to this account.
    if contact is None or contact.user_id != update.effective_user.id:
        await _reply(update, texts.PHONE_LATER_ACK, session)
        return
    try:
        await backend.post("/api/shop/bot/phone", json={
            "telegram_id": update.effective_user.id,
            "phone": contact.phone_number,
        })
    except ShopApiError:
        logger.exception("Could not save phone for %s", update.effective_user.id)
        await _reply(update, texts.generic_error(_handle(session)), session)
        return
    await _reply(update, texts.PHONE_SAVED, session)


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
    await update.effective_message.reply_text(text, reply_markup=main_menu(session), **kwargs)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    try:
        session = await _session(update)
    except ShopApiError:
        logger.exception("Could not open a shop session for %s", update.effective_user.id)
        # With the menu: a first-time visitor has never seen a keyboard, so a
        # bare error leaves them with an empty screen and nothing to tap.
        await update.effective_message.reply_text(texts.generic_error(None), reply_markup=main_menu(None))
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
    await _maybe_offer_phone_share(update, context, session)


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
    keyboard.append([KeyboardButton(texts.CUSTOM_VOLUME)])
    keyboard.append([KeyboardButton(texts.CANCEL)])
    await update.effective_message.reply_text(
        texts.buy_prompt(
            session["price_per_gb"], session["min_gb"], session["max_gb"],
            session["plan_duration_days"],
        ),
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )


async def on_renew(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The renew button under an expiry or usage warning.

    It exists because the gap between "your service ends in three days" and
    actually renewing is where the customer is lost: they read the message,
    mean to deal with it later, and later is after it stopped working. One tap
    puts them straight on the payment screen for the same plan they had.
    """
    query = update.callback_query
    await query.answer()
    try:
        volume = float(query.data.split(":", 1)[1])
    except (AttributeError, IndexError, ValueError):
        return
    # The button is removed once used, so an old warning cannot open a second
    # order days later.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Could not clear the renew button", exc_info=True)

    context.user_data.clear()
    session = await _session(update)
    if session["is_blocked"]:
        await _reply(update, texts.blocked(_handle(session)), session)
        return
    if not session["is_open"]:
        await _reply(update, texts.shop_closed(_handle(session)), session)
        return
    await _handle_volume(update, context, volume)


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
        await update.effective_message.reply_text(texts.out_of_range(session["min_gb"], session["max_gb"]))
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
        await update.effective_message.reply_text(
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
    await update.effective_message.reply_text(
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
        await backend.post(f"/api/shop/bot/orders/{order_id}/pay",
                           json={"telegram_id": update.effective_user.id}, timeout=120)
    except ShopApiError:
        logger.exception("Wallet purchase failed for order %s", order_id)
        await _reply(update, texts.generic_error(_handle(session)), session)
        return
    # Delivery is pushed by the backend — the same path an approved card
    # payment takes, so the customer receives an identical message either way.
    await _maybe_offer_phone_share(update, context, session)


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
    await update.effective_message.reply_text(
        texts.topup_ask_amount(session["min_topup"], session["max_topup"]),
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton(texts.CANCEL)]], resize_keyboard=True),
    )


async def _handle_topup_amount(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float) -> None:
    session = await _session(update)
    value = int(amount)
    if not (session["min_topup"] <= value <= session["max_topup"]):
        await update.effective_message.reply_text(
            texts.topup_out_of_range(session["min_topup"], session["max_topup"])
        )
        return
    context.user_data[_STATE] = _STATE_AWAITING_RECEIPT
    context.user_data[_PENDING_AMOUNT] = value
    # No order attached: this is a plain wallet top-up.
    context.user_data.pop(_ORDER_ID, None)
    await update.effective_message.reply_text(
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


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A receipt sent as a FILE rather than a photo.

    Telegram sends an image as a document whenever the sender picks "send as
    file", and some banking apps save receipts as PDF. Neither used to reach
    any handler at all, so the customer got silence after paying — the worst
    possible moment for the bot to say nothing.
    """
    doc = update.message.document
    mime = (doc.mime_type or "") if doc else ""
    if mime.startswith("image/"):
        await handle_photo(update, context, file_id=doc.file_id)
        return
    session = await _session(update)
    await _reply(update, texts.RECEIPT_AS_FILE, session)


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Voice notes, stickers, locations, contacts — anything with no meaning
    here. Answering is the point: silence reads as a broken bot, especially to
    someone who has just sent money."""
    session = await _session(update)
    await _reply(update, texts.not_understood(_handle(session)), session)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       *, file_id: str | None = None) -> None:
    await _submit_receipt(update, context, receipt_file_id=file_id or update.message.photo[-1].file_id)


# A typed tracking code instead of a photo — some banking apps make a
# screenshot awkward, and the operator makes the same manual call either
# way (see backend/app/models.py's ShopTopup.receipt_text). This is NOT
# meant to admit arbitrary chat — someone typing "سلام" or "چقدر شد؟" while
# the bot is waiting for a receipt is asking a real question, not sending
# one, and silently opening a pending top-up for it would burn the
# operator's attention on nothing. A plausible tracking code is short,
# mixed with the transfer's own reference digits, never a full sentence.
_MIN_RECEIPT_TEXT_LEN = 5
_MIN_RECEIPT_TEXT_DIGITS = 4


def _looks_like_receipt_text(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < _MIN_RECEIPT_TEXT_LEN:
        return False
    digit_count = sum(1 for ch in stripped.translate(_DIGITS) if ch.isdigit())
    return digit_count >= _MIN_RECEIPT_TEXT_DIGITS


async def _submit_receipt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    receipt_file_id: str | None = None,
    receipt_text: str | None = None,
) -> None:
    """Shared by a photo receipt and a typed one — everything past "what
    counts as proof" is identical: same pending-order recovery, same
    one-at-a-time guard, same failure messages."""
    session = await _session(update)
    amount = context.user_data.get(_PENDING_AMOUNT)
    order_id = context.user_data.get(_ORDER_ID)
    volume = context.user_data.get(_PENDING_VOLUME)

    # `is None`, not falsy: an amount of zero is still a live conversation.
    if context.user_data.get(_STATE) != _STATE_AWAITING_RECEIPT or amount is None:
        recovered = await _recover_pending_order(update)
        if recovered is None:
            # Genuinely nothing waiting. Says what it needs rather than
            # silently ignoring the receipt — an ignored one is a customer
            # who believes they have paid, waiting for a service nobody is
            # making.
            await _reply(update, texts.RECEIPT_WITHOUT_CONTEXT, session)
            return
        amount = recovered["shortfall"]
        order_id = recovered["order_id"]
        volume = recovered["data_limit_gb"]

    # Cleared before the call: a second receipt sent while this one is in
    # flight must not open a second payment against the same order.
    context.user_data.clear()

    try:
        topup = await backend.post("/api/shop/bot/topups", json={
            "telegram_id": update.effective_user.id,
            "claimed_amount": amount,
            "receipt_file_id": receipt_file_id,
            "receipt_text": receipt_text,
            "order_id": order_id,
        }, timeout=60)
    except ShopApiError as exc:
        if getattr(exc, "status", 0) == 409:
            # Their first receipt is still in the queue. Telling them to send
            # it again would produce exactly the duplicate this refused.
            await _reply(update, texts.RECEIPT_ALREADY_WAITING, session)
            return
        logger.exception("Could not record a receipt for %s", update.effective_user.id)
        # Specific, not the generic error: the customer's question here is
        # "did I just lose my money?", and the true answer is no — nothing was
        # recorded, so resending the same receipt is safe.
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
        await update.effective_message.reply_text(texts.generic_error(None))
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
        await update.effective_message.reply_text(texts.generic_error(None))
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
            seconds = (expires - datetime.now(timezone.utc)).total_seconds()
            # Rounded UP: .days truncates, so a service with ten hours left
            # came out as 0 and was shown to its owner as finished.
            days_left = -(-int(seconds) // 86400) if seconds > 0 else 0
        lines = [texts.account_line(limit_gb, used_gb, days_left)]
        if row["subscription_url"]:
            # On its own line: a Latin URL inline with Persian text gets
            # reordered by bidirectional rendering and can be copied wrong.
            lines.append("")
            lines.append(row["subscription_url"])
        await update.effective_message.reply_text("\n".join(lines))

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

    if text == texts.PHONE_LATER:
        context.user_data.clear()
        session = await _session(update)
        await _reply(update, texts.PHONE_LATER_ACK, session)
        return

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
        if text == texts.CUSTOM_VOLUME:
            # Stays in the same state: the next message is read as the number.
            await update.effective_message.reply_text(texts.ASK_CUSTOM_VOLUME)
            return
        volume = parse_number(text)
        if volume is None:
            await update.effective_message.reply_text(texts.not_a_number("۳۵"))
            return
        return await _handle_volume(update, context, volume)

    if state == _STATE_CHOOSING_TOPUP:
        amount = parse_number(text)
        if amount is None:
            await update.effective_message.reply_text(texts.not_a_number("۲۰۰,۰۰۰"))
            return
        return await _handle_topup_amount(update, context, amount)

    if state == _STATE_AWAITING_RECEIPT:
        # A plausible tracking code is accepted as the receipt itself — see
        # _submit_receipt. Anything else (a question, "واریز کردم" with
        # nothing to identify it by) gets an answer rather than silently
        # dropped, but does NOT open a pending top-up: that would burn the
        # operator's attention on something that isn't proof of anything.
        if _looks_like_receipt_text(text):
            return await _submit_receipt(update, context, receipt_text=text.strip())
        await update.effective_message.reply_text(texts.NEED_PHOTO)
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
