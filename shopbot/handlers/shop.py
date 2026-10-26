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
_STATE_CONFIRMING_PAYG_START = "confirming_payg_start"
_STATE_CHOOSING_TOPUP = "choosing_topup"
_STATE_AWAITING_RECEIPT = "awaiting_receipt"
_STATE_AWAITING_OPTIONAL_USERNAME = "awaiting_optional_username"
_STATE_AWAITING_PAYG_USERNAME = "awaiting_payg_username"
_PENDING_PAYG_PAYLOAD = "pending_payg_payload"
_PENDING_INTENT = "pending_intent"
_STATE_AWAITING_SERVICE_NAME = "awaiting_service_name"
_SERVICE_NAME_ACCOUNT = "service_name_account_id"
_STATE_AWAITING_OPTIONAL_NAME = "awaiting_optional_name"
_PENDING_SERVICE_NAME = "pending_service_name"
_PENDING_VOLUME_VALUE = "pending_volume_value"
_PENDING_FORCE_NEW = "pending_force_new"
SKIP_NAME = "بدون نام"
_ORDER_ID = "order_id"
_PENDING_AMOUNT = "pending_amount"
# Council 2026-10-24 (routed books): the receipt being sent is the customer's
# DESIGNATED debt payment — approval posts a ledger credit, not wallet credit.
_PENDING_FOR_DEBT = "pending_for_debt"
# Set while choosing a volume via MENU_NEW_SERVICE instead of MENU_BUY -
# read once, in _handle_volume, to tell the backend not to extend whatever
# account the customer already has.
_FORCE_NEW_SERVICE = "force_new_service"
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

# The daily payg meter debits share entry_type "purchase" with real orders,
# so the type alone labels them «خرید سرویس» — one line per day, reading as
# though the customer kept buying a new service daily. They carry the
# backend's own note instead: English "Payg meter: …" today, «بیل مصرف
# روزانه…» once the backend's own relabel lands — matched case-insensitively
# against both so the label is right whichever side deploys first.
_METER_NOTE_MARKERS = ("payg meter", "بیل مصرف روزانه")
_WALLET_METER_LABEL = "بیل مصرف روزانه"


def _wallet_label(entry: dict) -> str:
    if entry.get("type") == "purchase":
        note = (entry.get("note") or "").strip().lower()
        if any(marker in note for marker in _METER_NOTE_MARKERS):
            return _WALLET_METER_LABEL
    return _WALLET_LABELS.get(entry.get("type"), "تراکنش")

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
    # Routed-books debt (council 2026-10-24): its own row ABOVE nothing and
    # BELOW the wallet row — visible for anyone who owes, without reading as
    # a second "start here" next to MENU_BUY.
    if session and session.get("ledger_debt", 0) > 0:
        rows.append([KeyboardButton(texts.MENU_PAY_DEBT)])
    # Its own row, below the common actions - deliberately not next to
    # MENU_BUY so it never reads as "the normal way to buy" (see texts.py).
    rows.append([KeyboardButton(texts.MENU_NEW_SERVICE)])
    if session and session.get("payg_enabled"):
        rows.append([KeyboardButton(texts.MENU_PAYG)])
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
    # Shop-link deep link (see models.Customer's shop-link section on the
    # backend): /start shoplnk_<token> binds this Telegram account to the
    # EXISTING customer the operator invited, instead of opening the shop as
    # a stranger. A failed claim must NOT fall through to the normal welcome
    # — someone holding a dead link is exactly who the ordinary welcome (buy
    # something new) would leave confused about where their real service went.
    args = list(context.args or [])
    if args and args[0].startswith("shoplnk_"):
        return await _claim_link(update, args[0][len("shoplnk_"):])
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


async def _claim_link(update: Update, token: str) -> None:
    """POST /api/shop/bot/claim-link with the token from the deep link plus
    this Telegram account's identity — the mirror of delegate_bot's
    _claim_invite. Backend outcomes: 200 = bound, the payload names the
    customer and how many services came across; 404 = unknown/used/expired
    token; 409 = the binding would contradict an existing one (this Telegram
    already carries another customer's services, or the other way round).
    Anything else is a genuine server problem -> generic_error. Backend text
    is English for the operator and never shown — same rule as everywhere
    else in this bot."""
    user = update.effective_user
    try:
        result = await backend.post("/api/shop/bot/claim-link", json={
            "token": token,
            "telegram_id": user.id,
            "telegram_username": user.username,
        })
    except ShopApiError as exc:
        if exc.status == 404:
            await update.effective_message.reply_text(texts.LINK_INVALID_OR_EXPIRED)
        elif exc.status == 409:
            await update.effective_message.reply_text(texts.LINK_ALREADY_CONNECTED)
        else:
            await update.effective_message.reply_text(texts.generic_error(None))
        return
    await update.effective_message.reply_text(
        texts.link_welcome(result["customer_name"], result["accounts_linked"]),
        reply_markup=main_menu(None),
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


# ── pay-as-you-go ("📊 سرویس مصرفی") ───────────────────────────────────────


async def start_payg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A real, ongoing billing relationship (even though no money moves on
    this tap), so it gets its own confirm step like a purchase — unlike the
    free, one-time trial above."""
    context.user_data.clear()
    session = await _session(update)
    if session["is_blocked"]:
        await _reply(update, texts.blocked(_handle(session)), session)
        return
    if not session.get("payg_enabled"):
        await _reply(update, texts.PAYG_UNAVAILABLE, session)
        return

    context.user_data[_STATE] = _STATE_AWAITING_PAYG_USERNAME
    context.user_data[_PENDING_PAYG_PAYLOAD] = {
        "telegram_username": update.effective_user.username,
        "display_name": " ".join(filter(None, [update.effective_user.first_name,
                                               update.effective_user.last_name])) or None,
    }
    # The username ask rides the SAME screen as the billing intro: one
    # message, one optional answer, same named-escape discipline.
    await update.effective_message.reply_text(
        texts.payg_intro(session["payg_rate_per_gb"]) + "\n\n" + texts.ASK_OPTIONAL_USERNAME,
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton(texts.SKIP_NAME)], [KeyboardButton(texts.CANCEL)]],
            resize_keyboard=True,
        ),
    )


async def _payg_username_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The answer to the PAYG username ask: skip = auto shop{id}, a Latin
    name = the customer's own. Anything else is the answer — the state
    branch runs before the menu dispatch, so a stray tap cannot wipe it."""
    text = (update.message.text or "").strip()
    payload = context.user_data.get(_PENDING_PAYG_PAYLOAD) or {}
    if text == texts.CANCEL:
        context.user_data.clear()
        session = None
        try:
            session = await _session(update)
        except ShopApiError:
            pass
        await update.effective_message.reply_text(
            texts.PURCHASE_CANCELLED,
            reply_markup=main_menu(session),
        )
        return
    if text in (texts.MENU_BUY, texts.MENU_NEW_SERVICE, texts.MENU_ACCOUNTS,
                texts.MENU_WALLET, texts.MENU_TOPUP, texts.MENU_PAYG,
                texts.MENU_TRIAL, texts.MENU_HELP):
        context.user_data.clear()
        return await handle_text(update, context)  # re-dispatch as a menu tap
    chosen = None if text == texts.SKIP_NAME else text.strip()
    if chosen and not re.match(r"^[a-zA-Z0-9_-]{3,30}$", chosen):
        await update.effective_message.reply_text(texts.USERNAME_INVALID)
        return
    payload["marzban_username"] = chosen
    context.user_data.clear()
    session = None
    try:
        session = await _session(update)
    except ShopApiError:
        pass
    await update.effective_message.reply_text(texts.BUY_WORKING,
                                              reply_markup=main_menu(session))
    user = update.effective_user
    payload.update({"telegram_id": user.id, "telegram_username": user.username,
                    "display_name": " ".join(filter(None, [user.first_name, user.last_name])) or None})
    try:
        await backend.post("/api/shop/bot/payg/start", json=payload, timeout=90)
    except ShopApiError as exc:
        t = str(exc)
        if "ALREADY_HAS_SERVICE" in t:
            await _reply(update, texts.PAYG_ALREADY_HAS_SERVICE, session)
        elif "PAYG_NOT_AVAILABLE" in t:
            await _reply(update, texts.PAYG_UNAVAILABLE, session)
        elif "نام کاربری" in t:
            # taken/invalid — the ask re-opens with the reason
            await update.effective_message.reply_text(t)
            context.user_data[_STATE] = _STATE_AWAITING_PAYG_USERNAME
            context.user_data[_PENDING_PAYG_PAYLOAD] = {
                "telegram_username": user.username,
                "display_name": " ".join(filter(None, [user.first_name, user.last_name])) or None,
            }
            return
        else:
            logger.exception("Payg start failed for %s", user.id)
            await _reply(update, texts.generic_error(_handle(session)), session)
        return
    await update.effective_message.reply_text(texts.PAYG_STARTED_CONFIRM)


async def _confirm_payg_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    session = await _session(update)
    await _reply(update, texts.BUY_WORKING, session)
    user = update.effective_user
    try:
        await backend.post("/api/shop/bot/payg/start", json={
            "telegram_id": user.id,
            "telegram_username": user.username,
            "display_name": " ".join(filter(None, [user.first_name, user.last_name])) or None,
        }, timeout=90)
    except ShopApiError as exc:
        text = str(exc)
        if "ALREADY_HAS_SERVICE" in text:
            await _reply(update, texts.PAYG_ALREADY_HAS_SERVICE, session)
        elif "PAYG_NOT_AVAILABLE" in text:
            await _reply(update, texts.PAYG_UNAVAILABLE, session)
        else:
            logger.exception("Payg start failed for %s", user.id)
            await _reply(update, texts.generic_error(_handle(session)), session)
        return
    # No success message here either — the backend's delivery push says it
    # better, same reasoning as take_trial above.
    await _maybe_offer_phone_share(update, context, session)


# ── buying, order first ───────────────────────────────────────────────────


async def start_buy(update: Update, context: ContextTypes.DEFAULT_TYPE, *, force_new: bool = False) -> None:
    context.user_data.clear()
    session = await _session(update)
    if session["is_blocked"]:
        await _reply(update, texts.blocked(_handle(session)), session)
        return
    if not session["is_open"]:
        await _reply(update, texts.shop_closed(_handle(session)), session)
        return

    context.user_data[_STATE] = _STATE_CHOOSING_VOLUME
    if force_new:
        context.user_data[_FORCE_NEW_SERVICE] = True
    quick = [v for v in QUICK_VOLUMES if session["min_gb"] <= v <= session["max_gb"]]
    keyboard = [[KeyboardButton(texts.gb(v)) for v in quick[i:i + 3]] for i in range(0, len(quick), 3)]
    keyboard.append([KeyboardButton(texts.CUSTOM_VOLUME)])
    keyboard.append([KeyboardButton(texts.CANCEL)])
    prompt = texts.buy_prompt(
        session["price_per_gb"], session["min_gb"], session["max_gb"],
        session["plan_duration_days"],
    )
    if force_new:
        prompt = texts.NEW_SERVICE_INTRO + "\n\n" + prompt
    await update.effective_message.reply_text(
        prompt,
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )


async def start_new_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_buy(update, context, force_new=True)


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


async def on_auto_renew(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The "⚡ تمدید خودکار (میانگین مصرفم)" button on the usage warning —
    one tap, no volume to pick: the backend renews by this account's own
    average and may take the wallet negative (bounded, see
    shop_service.auto_renew_account). This is a direct backend call, not
    _handle_volume — there is no order to create-then-confirm, the backend
    does everything in one step."""
    query = update.callback_query
    await query.answer()
    try:
        account_id = int(query.data.split(":", 1)[1])
    except (AttributeError, IndexError, ValueError):
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Could not clear the auto-renew button", exc_info=True)

    context.user_data.clear()
    session = await _session(update)
    if session["is_blocked"]:
        await _reply(update, texts.blocked(_handle(session)), session)
        return

    await _reply(update, texts.BUY_WORKING, session)
    try:
        result = await backend.post(
            f"/api/shop/bot/accounts/{account_id}/auto-renew",
            json={"telegram_id": update.effective_user.id},
        )
    except ShopApiError as exc:
        text = str(exc)
        if "ALREADY_IN_DEBT" in text:
            await _reply(update, texts.AUTO_RENEW_ALREADY_IN_DEBT, session)
        elif "NO_HISTORY" in text:
            await _reply(update, texts.AUTO_RENEW_NO_HISTORY, session)
        else:
            logger.exception("Auto-renew failed for account %s", account_id)
            await _reply(update, texts.generic_error(_handle(session)), session)
        return

    if result.get("went_negative"):
        await _reply(
            update,
            texts.auto_renew_in_debt(result["data_limit_gb"], result["owed"], result["grace_days"]),
            session,
        )
    else:
        await _reply(update, texts.auto_renew_ok(result["data_limit_gb"]), session)


def _payment_request_text(intent: dict, ask: int | None = None) -> str:
    """The plan, then the one number to transfer.

    When the wallet already holds something, that part is stated separately so
    the figure being asked for is unambiguous — the customer types the
    shortfall into their bank app, not the price.

    `ask` is the figure the customer is actually asked to transfer. It is the
    shortfall, RAISED to the shop's minimum top-up when the shortfall is
    smaller (F10): an order-bound receipt is accepted at any amount by the
    backend, but a card-to-card under the shop's own floor is the transfer
    Iranian banking apps make painful, so the ask clears the floor and the
    text promises the surplus back to the wallet — which the approval
    literally credits. None means "the shortfall as-is" (the old shape).
    """
    head = f"🛒 {texts.gb(intent['data_limit_gb'])} — {texts.fa(intent['duration_days'])} روزه\n"
    if intent["balance"] > 0:
        head += (
            f"قیمت: {texts.money(intent['price'])}\n"
            f"از کیف پولتان کم می‌شود: {texts.money(intent['balance'])}\n"
        )
    if ask is not None and ask > intent["shortfall"]:
        return head + "\n" + texts.topup_instructions_with_minimum(
            ask, intent["shortfall"], intent["card_number"], intent.get("card_holder"),
            intent.get("approval_eta_minutes", 30),
        )
    return head + "\n" + texts.topup_instructions(
        intent["shortfall"], intent["card_number"], intent.get("card_holder"),
        intent.get("approval_eta_minutes", 30),
    )


async def _handle_volume(update: Update, context: ContextTypes.DEFAULT_TYPE, volume: float) -> None:
    """Volume chosen. Creates the ORDER — which takes no money — then, when
    this purchase MINTS a new user, asks for the customer's CHOSEN Marzban
    username (optional; skip = the auto shop{id} name) before the payment
    screen. Extensions cannot rename, so they skip the ask entirely."""
    session = await _session(update)
    if not (session["min_gb"] <= volume <= session["max_gb"]):
        await update.effective_message.reply_text(texts.out_of_range(session["min_gb"], session["max_gb"]))
        return

    try:
        intent = await backend.post("/api/shop/bot/orders", json={
            "telegram_id": update.effective_user.id,
            "data_limit_gb": volume,
            "force_new_service": bool(context.user_data.get(_FORCE_NEW_SERVICE)),
        })
    except ShopApiError as exc:
        if getattr(exc, "status", 0) == 409 and "SERVICE_IS_UNLIMITED" in str(exc):
            # Their current service has no cap — a volume purchase has nothing
            # to add to it, and the backend refused before any money moved.
            # This is a "you don't need this", not an error.
            context.user_data.clear()
            await _reply(update, texts.SERVICE_UNLIMITED, session)
            return
        if getattr(exc, "status", 0) == 409 and "PAYG_TOPUP_INSTEAD" in str(exc):
            # F3(b): their current service is METERED — the backend refuses a
            # fixed-volume package on it (before any money moves) and wants a
            # wallet top-up instead. A "buy the other way", not an error.
            context.user_data.clear()
            await _reply(update, texts.PAYG_TOPUP_INSTEAD, session)
            return
        logger.exception("Could not create an order for %s", update.effective_user.id)
        context.user_data.clear()
        await _reply(update, texts.generic_error(_handle(session)), session)
        return

    context.user_data[_ORDER_ID] = intent["order_id"]
    context.user_data[_PENDING_VOLUME] = intent["data_limit_gb"]

    if intent.get("creates_new"):
        # F-E2 (owner 2026-10-24): a NEW service carries the customer's CHOSEN
        # username — the name their VPN app and subscription actually show —
        # instead of an auto shop10. Optional (skip = auto), and the answer
        # passes a live availability check before any payment moves. The
        # state answers BEFORE the menu dispatch (see handle_text): a menu
        # tap mid-ask used to wipe it silently.
        context.user_data[_STATE] = _STATE_AWAITING_OPTIONAL_USERNAME
        context.user_data[_PENDING_INTENT] = intent
        await update.effective_message.reply_text(
            texts.ASK_OPTIONAL_USERNAME,
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton(texts.SKIP_NAME)], [KeyboardButton(texts.CANCEL)]],
                resize_keyboard=True,
            ),
        )
        return

    return await _payment_screens(update, context, intent)


async def _payment_screens(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           intent: dict) -> None:
    """The two payment screens (one-tap wallet confirm / card-to-card receipt
    ask), driven by the order intent. Split from the order creation so the
    optional-username step can sit between them."""
    session = await _session(update)

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

    # F10: never steer a customer into a card-to-card under the shop's own
    # minimum — ask for the floor instead and let the surplus return to the
    # wallet (the backend accepts an order-bound receipt at any amount, and
    # the approval credits the remainder). With min_topup at 0 ("no minimum")
    # this is the shortfall unchanged.
    ask = max(int(intent["shortfall"]), int(session.get("min_topup") or 0))
    context.user_data[_STATE] = _STATE_AWAITING_RECEIPT
    context.user_data[_PENDING_AMOUNT] = ask
    await update.effective_message.reply_text(
        _payment_request_text(intent, ask),
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
    except ShopApiError as exc:
        logger.exception("Wallet purchase failed for order %s", order_id)
        if getattr(exc, "status", 0) == 502:
            # F12: the backend refunds the wallet BEFORE answering 502 (the
            # account build failed). The generic "کاری انجام نشد" would leave
            # someone who just saw a wallet debit assuming the money is gone;
            # this text answers the question they actually hold.
            await _reply(update, texts.PURCHASE_FAILED_REFUNDED, session)
            return
        detail = str(exc)
        if detail.startswith("DEBT_LINE:"):
            # The backend's refusal is already the customer's Persian
            # sentence (council 2026-10-24) — relay it, minus the routing
            # marker the bot keys on.
            await _reply(update, detail[len("DEBT_LINE:"):].strip(), session)
            return
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
    debt = session.get("ledger_debt", 0)
    prefix = (texts.debt_line(debt) + "\n\n") if debt > 0 else ""
    await update.effective_message.reply_text(
        prefix + texts.topup_ask_amount(session["min_topup"], session["max_topup"]),
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton(texts.CANCEL)]], resize_keyboard=True),
    )


async def _handle_topup_amount(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float) -> None:
    session = await _session(update)
    value = int(amount)
    for_debt = bool(context.user_data.get(_PENDING_FOR_DEBT))
    # A debt payment has no wallet floor (min_topup is a wallet-product rule);
    # it still respects the anti-fat-finger ceiling.
    floor = 1000 if for_debt else session["min_topup"]
    if not (floor <= value <= session["max_topup"]):
        await update.effective_message.reply_text(
            texts.topup_out_of_range(floor, session["max_topup"])
        )
        return
    context.user_data[_STATE] = _STATE_AWAITING_RECEIPT
    context.user_data[_PENDING_AMOUNT] = value
    # No order attached: this is a plain wallet top-up (or a debt payment,
    # whose flag _PENDING_FOR_DEBT already rides user_data).
    context.user_data.pop(_ORDER_ID, None)
    if for_debt:
        await update.effective_message.reply_text(
            texts.debt_receipt_instructions(
                value, session["card_number"], session.get("card_holder"),
                session.get("approval_eta_minutes", 30),
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton(texts.CANCEL)]], resize_keyboard=True),
        )
        return
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
    lines = [f"✅ رسیدتان با موفقیت دریافت شد{what}."]
    if has_order:
        lines.append(f"معمولاً تا {texts.fa(eta)} دقیقه بررسی و سرویس‌تان خودکار آماده می‌شود.")
    else:
        lines.append(f"معمولاً تا {texts.fa(eta)} دقیقه بررسی و کیف پولتان خودکار شارژ می‌شود.")
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
    for_debt = bool(context.user_data.get(_PENDING_FOR_DEBT))

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
            "for_debt": for_debt,
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

    if for_debt:
        await _reply(
            update,
            texts.debt_receipt_done(topup.get("reference_code"), session.get("approval_eta_minutes", 30)),
            session,
        )
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


# ── paying the statement debt (council 2026-10-24, routed books) ──────────


async def start_pay_debt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The debt landing: status lines, then the two ways to pay — from the
    wallet (the single sanctioned cross-system write, one tap) or by a
    card-to-card receipt DESIGNATED as a debt payment."""
    context.user_data.clear()
    session = await _session(update)
    debt = session.get("ledger_debt", 0)
    if debt <= 0:
        await _reply(update, texts.DEBT_NONE, session)
        return
    text = texts.debt_status(debt, session["balance"], session.get("approval_eta_minutes", 30))
    if session["balance"] > 0:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                text=f"💳 پرداخت از کیف پول ({texts.money(min(session['balance'], int(debt)))})",
                callback_data="debt:wallet",
            )],
            [InlineKeyboardButton(text=texts.DEBT_PAY_BY_RECEIPT, callback_data="debt:receipt")],
        ])
        await update.effective_message.reply_text(text, reply_markup=keyboard)
        return
    await _start_debt_receipt(update, context, session, debt)


async def _start_debt_receipt(update, context, session, debt) -> None:
    context.user_data[_STATE] = _STATE_CHOOSING_TOPUP
    context.user_data[_PENDING_FOR_DEBT] = True
    await update.effective_message.reply_text(
        f"چند تومان می‌خواهید از بدهیِ {texts.money(int(debt))} بپردازید؟"
        f"\n(حداقل ۱٬۰۰۰ — حداکثر {texts.money(session['max_topup'])})",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton(texts.CANCEL)]], resize_keyboard=True),
    )


async def on_pay_debt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The inline keyboard's two entries. Menu buttons cancel any ask and run
    their own flow — the same resilience rule as the name steps."""
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    session = await _session(update)
    debt = session.get("ledger_debt", 0)
    if debt <= 0:
        await query.edit_message_text(texts.DEBT_NONE)
        return
    if query.data == "debt:wallet":
        pay = min(session["balance"], int(debt))
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        await query.edit_message_text(
            texts.debt_status(debt, session["balance"], session.get("approval_eta_minutes", 30))
            + f"\n\nپرداخت {texts.money(pay)} از کیف پول؟",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(text=texts.DEBT_PAY_WALLET_CONFIRM, callback_data="debt:wallet:yes"),
                InlineKeyboardButton(text=texts.CANCEL, callback_data="debt:cancel"),
            ]]),
        )
        return
    # debt:receipt
    await _start_debt_receipt(update, context, session, debt)


async def on_pay_debt_wallet_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    session = await _session(update)
    if session.get("ledger_debt", 0) <= 0 or session["balance"] <= 0:
        await query.edit_message_text(texts.DEBT_NONE if session.get("ledger_debt", 0) <= 0 else "کیف پولتان خالی است — از «ارسال رسید» استفاده کنید.")
        return
    try:
        result = await backend.post("/api/shop/bot/pay-debt-from-wallet",
                                    json={"telegram_id": update.effective_user.id}, timeout=60)
    except ShopApiError as exc:
        detail = str(exc) or ""
        await query.edit_message_text(detail.removeprefix("ShopApiError: ") if detail else texts.generic_error(_handle(session)))
        return
    await query.edit_message_text(
        texts.debt_paid_from_wallet(result["paid"], result["wallet_balance"], result["ledger_debt"])
    )


async def on_pay_debt_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    try:
        session = await _session(update)
    except ShopApiError:
        await query.edit_message_text("انصراف شد.")
        return
    await query.edit_message_text("انصراف شد. منوی اصلی همین‌جاست.")


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
            label = _wallet_label(entry)
            lines.append(f"{label}: {texts.money(abs(entry['amount']))}")
    await _reply(update, "\n".join(lines), session)


def _display_service_name(row: dict) -> str | None:
    """The title a service is listed under: the customer's own nickname, else
    the panel username of a linked reseller account. Those never had a
    nickname set, so every one of them was an anonymous «سرویس ۳۰ گیگ» —
    indistinguishable when a customer owns several (owner report 2026-10-25).
    «—» is the backend's placeholder for an order with no panel user yet."""
    nick = (row.get("service_name") or "").strip()
    if nick:
        return nick
    # Only for accounts the operator created (source "linked"): a shop-bought
    # service carries an auto-generated panel name («shop10») that means nothing
    # to the customer — owner feedback 2026-10-26.
    if row.get("source") != "linked":
        return None
    username = (row.get("marzban_username") or "").strip()
    return username if username and username != "—" else None


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
        # F11: the backend's row carries the account's live status — a
        # suspended service must say so instead of rendering as "active, X GB
        # left" that secretly connects to nothing. Passing it changes nothing
        # for active/limited/expired rows (see account_line's default).
        lines = [texts.account_line(limit_gb, used_gb, days_left,
                                     status=row.get("status"), name=_display_service_name(row))]
        # An inline URL BUTTON, not the raw link: subscription URLs churn on
        # every panel-side modify (live 2026-10-01 — three different tokens
        # in one hour), and a Latin URL printed inside Persian text gets
        # bidi-reordered and copied wrong. A button always opens the CURRENT
        # url without pasting anything into the chat. The nickname button
        # (owner feature 2026-10-23) rides the same keyboard — ownership is
        # re-checked server-side at save time; the button only opens the ask.
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        buttons = []
        if row["subscription_url"]:
            buttons.append([InlineKeyboardButton("🔗 لینک اشتراک", url=row["subscription_url"])])
        if row.get("account_id"):
            buttons.append([InlineKeyboardButton("✏️ نام سرویس", callback_data=f"svcname:{row['account_id']}")])
        keyboard = InlineKeyboardMarkup(buttons) if buttons else None
        await update.effective_message.reply_text("\n".join(lines), reply_markup=keyboard)

    await _reply(update, texts.wallet_summary(session["balance"]), session)


async def on_service_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The ✏️ نام button on a service: asks for the customer's own nickname.
    Ownership is validated SERVER-side when the name is saved (the callback
    only opens the ask); the id rides in the callback data."""
    query = update.callback_query
    await query.answer()
    account_id = query.data.split(":", 1)[1]
    context.user_data.clear()
    context.user_data[_STATE] = _STATE_AWAITING_SERVICE_NAME
    context.user_data[_SERVICE_NAME_ACCOUNT] = account_id
    await update.effective_message.reply_text(texts.ASK_SERVICE_NAME)


# ── routing ───────────────────────────────────────────────────────────────


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menu buttons are matched BEFORE any in-flight state.

    That ordering is the escape hatch: a customer halfway through a top-up who
    taps "🛒 خرید سرویس" gets the buy screen, not a complaint that their
    message isn't a number. Being unable to leave a flow except by /start was
    a real dead end in the previous version.
    """
    text = (update.message.text or "").strip()

    # The username ask answers BEFORE the menu dispatch — the live customer
    # report: a «سرویس‌های من» tap mid-ask wiped the pending state silently
    # (every handler starts with context.user_data.clear()). Named escapes
    # only: «انصراف» cancels, a menu button cancels AND runs its menu,
    # «بدون نام» takes the auto name, anything else is the chosen username.
    if context.user_data.get(_STATE) == _STATE_AWAITING_OPTIONAL_USERNAME:
        intent = context.user_data.get(_PENDING_INTENT) or {}
        order_id = context.user_data.get(_ORDER_ID)
        if text == texts.CANCEL:
            context.user_data.clear()
            session = None
            try:
                session = await _session(update)
            except ShopApiError:
                pass
            await update.effective_message.reply_text(
                texts.PURCHASE_CANCELLED,
                reply_markup=main_menu(session),
            )
            return
        if text in (texts.MENU_BUY, texts.MENU_NEW_SERVICE, texts.MENU_ACCOUNTS,
                    texts.MENU_WALLET, texts.MENU_TOPUP, texts.MENU_PAYG,
                    texts.MENU_TRIAL, texts.MENU_HELP):
            context.user_data.clear()
            return await handle_text(update, context)  # re-dispatch as a menu tap
        if text == texts.SKIP_NAME:
            chosen = None  # auto shop{id} name
        else:
            chosen = text.strip()
            # Instant local pattern check; the definitive availability check
            # runs server-side now, and AGAIN at create_user (a race refunds,
            # never double-charges).
            if not re.match(r"^[a-zA-Z0-9_-]{3,30}$", chosen):
                await update.effective_message.reply_text(texts.USERNAME_INVALID)
                return
        try:
            await backend.post(f"/api/shop/bot/orders/{order_id}/username",
                               json={"telegram_id": update.effective_user.id, "name": chosen or ""})
        except ShopApiError as exc:
            if getattr(exc, "status", 0) == 409:
                # Taken — the ask stays open so the customer tries another.
                await update.effective_message.reply_text(texts.username_taken(chosen))
                return
            logger.exception("Username set failed for %s (order %s)",
                             update.effective_user.id, order_id)
            await update.effective_message.reply_text(texts.generic_error(None))
            return
        context.user_data.clear()
        # The order binding MUST survive: the card path's receipt submission
        # posts its topup bound to this order id (order-first flow).
        context.user_data[_ORDER_ID] = order_id
        context.user_data[_PENDING_INTENT] = intent
        return await _payment_screens(update, context, intent)

    if context.user_data.get(_STATE) == _STATE_AWAITING_PAYG_USERNAME:
        return await _payg_username_answer(update, context)

    # The ✏️ rename ask ALSO answers before the menu dispatch — the same
    # live report: a «سرویس‌های من» tap mid-rename wiped the ask silently.
    # Menu buttons cancel the ask and run their menu; «حذف» clears the name;
    # anything else is the name.
    if context.user_data.get(_STATE) == _STATE_AWAITING_SERVICE_NAME:
        account_id = context.user_data.get(_SERVICE_NAME_ACCOUNT)
        name = text.strip()
        if name in ("حذف", "پاک کردن"):
            name = ""
        if name in (texts.MENU_BUY, texts.MENU_NEW_SERVICE, texts.MENU_ACCOUNTS,
                    texts.MENU_WALLET, texts.MENU_TOPUP, texts.MENU_PAYG,
                    texts.MENU_TRIAL, texts.MENU_HELP, texts.CANCEL):
            context.user_data.clear()
            return await handle_text(update, context)  # re-dispatch as a menu tap
        if len(name) > 40:
            await update.effective_message.reply_text(texts.SERVICE_NAME_TOO_LONG)
            return
        context.user_data.clear()
        try:
            session = await _session(update)
        except ShopApiError:
            logger.exception("Session failed during a service rename for %s", update.effective_user.id)
            await update.effective_message.reply_text(texts.generic_error(None))
            return
        try:
            await backend.post(
                f"/api/shop/bot/accounts/{account_id}/name",
                json={"telegram_id": update.effective_user.id, "name": name},
            )
        except ShopApiError:
            logger.exception("Service rename failed for %s (account %s)",
                             update.effective_user.id, account_id)
            await update.effective_message.reply_text(texts.generic_error(session))
            return
        await update.effective_message.reply_text(
            texts.SERVICE_NAME_CLEARED if not name else texts.SERVICE_NAME_SAVED)
        return

    # The optional service-name step answers BEFORE the menu dispatch — the
    # live 2026-10-24 report: the ask was silently wiped when the customer
    # tapped «سرویس‌های من» mid-naming, because every handler starts with
    # context.user_data.clear(). Named escape hatches only.
    if context.user_data.get(_STATE) == _STATE_AWAITING_OPTIONAL_NAME:
        volume = context.user_data.get(_PENDING_VOLUME_VALUE)
        force_new = bool(context.user_data.get(_PENDING_FORCE_NEW))
        if text == texts.CANCEL:
            context.user_data.clear()
            session = None
            try:
                session = await _session(update)
            except ShopApiError:
                pass
            await update.effective_message.reply_text(
                texts.PURCHASE_CANCELLED,
                reply_markup=main_menu(session),
            )
            return
        name = "" if text == SKIP_NAME else text.strip()
        if len(name) > 40:
            await update.effective_message.reply_text(texts.SERVICE_NAME_TOO_LONG)
            return
        return await _create_order_and_ask_payment(
            update, context, volume, name, force_new=force_new)

    if text == texts.PHONE_LATER:
        context.user_data.clear()
        session = await _session(update)
        await _reply(update, texts.PHONE_LATER_ACK, session)
        return

    if text == texts.MENU_BUY:
        return await start_buy(update, context)
    if text == texts.MENU_NEW_SERVICE:
        return await start_new_service(update, context)
    if text == texts.MENU_PAYG:
        return await start_payg(update, context)
    if text == texts.MENU_TRIAL:
        return await take_trial(update, context)
    if text == texts.MENU_ACCOUNTS:
        return await show_accounts(update, context)
    if text == texts.MENU_WALLET:
        return await show_wallet(update, context)
    if text == texts.MENU_TOPUP:
        return await start_topup(update, context)
    if text == texts.MENU_PAY_DEBT:
        return await start_pay_debt(update, context)
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

    if state == _STATE_CONFIRMING_PAYG_START and text == texts.CONFIRM_BUY:
        return await _confirm_payg_start(update, context)

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
