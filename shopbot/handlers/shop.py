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
# T4: the OPTIONAL pre-pick of the new service's Marzban username, asked on
# the volume screen (never between the volume and the payment — the old
# post-order ask and its dead _STATE_AWAITING_OPTIONAL_NAME twin are gone).
_STATE_AWAITING_PREFERRED_NAME = "awaiting_preferred_name"
_PENDING_PREFERRED_NAME = "pending_preferred_name"
_STATE_AWAITING_PAYG_USERNAME = "awaiting_payg_username"
_PENDING_PAYG_PAYLOAD = "pending_payg_payload"
_STATE_AWAITING_SERVICE_NAME = "awaiting_service_name"
_SERVICE_NAME_ACCOUNT = "service_name_account_id"
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
# Set by a service card's renew button (ren:<account_id>:<gb>): the order must
# extend exactly THAT service. Only sent to a backend that advertises
# session["per_account_renewal"] - an older backend would drop the field and
# renew the wrong service.
_TARGET_ACCOUNT_ID = "target_account_id"
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
    "topup": "شارژ",
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
_WALLET_METER_LABEL = "مصرف روزانه"


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
    """The menu adapts to who is looking at it — layout A (T2, see
    docs/ZCODE_SHOPBOT_UX_DESIGN.md): at most four doors.

    A trial-eligible newcomer sees the trial FIRST and nothing about money or
    services they don't have yet. A returning customer gets buy/renew +
    services on one row, ONE money door («حساب من» — or the debt itself as the
    label when they owe), and help & support. Everything rarer (a second,
    separate service; the metered pay-as-you-go start) lives on the buy
    screen as a conditional button.

    The LEGACY labels (wallet / top-up / pay debt / new service / payg / help
    / support) are never rendered here again, but every one of them still
    routes — old chats still carry keyboards with those buttons on them.
    """
    trial = bool(session and session.get("trial_available"))
    has_accounts = bool(session and session.get("has_accounts"))
    debt = (session or {}).get("ledger_debt", 0)
    balance = (session or {}).get("balance", 0)
    rows: list[list[KeyboardButton]] = []
    if trial:
        # A stranger who can still take the trial: no accounts, no money
        # history (trial_available excludes existing customers) — the
        # smallest possible decision screen.
        rows.append([KeyboardButton(texts.MENU_TRIAL)])
        rows.append([KeyboardButton(texts.MENU_BUY)])
    else:
        buy_label = texts.MENU_BUY_RENEW if has_accounts else texts.MENU_BUY
        rows.append([KeyboardButton(buy_label), KeyboardButton(texts.MENU_ACCOUNTS)])
        if session is not None:
            rows.append([KeyboardButton(texts.menu_account_label(debt))])
    rows.append([KeyboardButton(texts.MENU_HELP_SUPPORT)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# Every label that routes as a menu tap — the NEW four doors, the LEGACY
# labels old chats still show, and the conditional buy-screen buttons. The
# mid-ask escape dispatches (username ask, payg ask, rename ask) and the main
# dispatcher all check against this one set, so a button tap can never be
# swallowed by an in-flight flow.
# NOT here on purpose: texts.CANCEL (every flow checks it explicitly, with
# its own cancel sentence) and texts.DEBT_AMOUNT_AS_IS (a FLOW button — it
# must be read by the top-up amount branch, not eaten by the menu dispatch).
_MENU_TEXTS = frozenset({
    texts.MENU_BUY, texts.MENU_BUY_RENEW, texts.MENU_NEW_SERVICE, texts.NEW_SERVICE_ON_BUY,
    texts.MENU_PAYG, texts.PAYG_ON_BUY, texts.MENU_TRIAL, texts.MENU_ACCOUNTS,
    texts.MENU_WALLET, texts.MENU_TOPUP, texts.MENU_PAY_DEBT, texts.MENU_ACCOUNT,
    texts.MENU_HELP, texts.MENU_SUPPORT, texts.MENU_HELP_SUPPORT,
    texts.PHONE_LATER,
})


def _is_menu_tap(text: str) -> bool:
    """A menu button's text — the fixed labels AND the debt-carrying «حساب
    من» variant, whose label embeds the amount so it cannot be a set
    member."""
    return text in _MENU_TEXTS or texts.is_menu_account_label(text)


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
    # T7: the menu rides UNDER the prompt — sharing a phone number must never
    # strand the customer on a two-button keyboard at the exact moment their
    # service just arrived (T0 trap #1).
    menu_rows = [[KeyboardButton(b.text) for b in row] for row in main_menu(session).keyboard]
    await update.effective_message.reply_text(
        texts.SHARE_PHONE_PROMPT,
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton(texts.SHARE_PHONE_BUTTON, request_contact=True),
              KeyboardButton(texts.PHONE_LATER)],
             *menu_rows],
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
    await show_help_support(update, context)


async def show_help_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The ONE help & support door (T2): how buying works and the human to
    reach — one screen instead of two sibling buttons. The /help command and
    both legacy labels land here too."""
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
    """Legacy «💬 پشتیبانی» label: the merged help & support screen."""
    await show_help_support(update, context)


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
        texts.payg_intro(session["payg_rate_per_gb"]) + "\n\n" + texts.ASK_PREFERRED_NAME,
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
    if _is_menu_tap(text):
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
    await _render_volume_screen(update, session, force_new=force_new)


async def _render_volume_screen(update: Update, session: dict, *, force_new: bool) -> None:
    """The volume choice itself. T4: INLINE buttons — the main menu stays
    visible on the reply keyboard underneath, and a typed volume is still
    accepted (the state branch in handle_text). The optional username pre-pick
    sits here too, only when this purchase can actually mint a new service."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    # R1-2 (review 2026-11-06): the volume callbacks carry the force-new flag
    # (`vol:30:f`) so a STALE screen — the customer tapped «سرویس جداگانه»,
    # then a restart wiped user_data — cannot silently retarget their next
    # volume tap at extending the EXISTING service. The plain form stays
    # valid (a round-1 screen's `vol:30` re-seeds a plain buy).
    suffix = ":f" if force_new else ""
    quick = [v for v in QUICK_VOLUMES if session["min_gb"] <= v <= session["max_gb"]]
    keyboard = [[InlineKeyboardButton(texts.gb(v), callback_data=f"vol:{v:g}{suffix}")
                 for v in quick[i:i + 3]] for i in range(0, len(quick), 3)]
    keyboard.append([InlineKeyboardButton(texts.CUSTOM_VOLUME, callback_data=f"vol:custom{suffix}")])
    # The rare starts live HERE, not in the main menu (T2): a second,
    # independent service only makes sense for someone who HAS a service (a
    # newcomer's first buy is already creates_new) — and so does the optional
    # username, which is why it shows under the same condition.
    if session.get("has_accounts") or force_new:
        keyboard.append([InlineKeyboardButton(texts.NEW_SERVICE_ON_BUY, callback_data="menu:newservice")])
    else:
        keyboard.append([InlineKeyboardButton(texts.PREFERRED_NAME_BUTTON, callback_data="uname:ask")])
    if session.get("payg_enabled") and not session.get("has_payg_service"):
        keyboard.append([InlineKeyboardButton(texts.PAYG_ON_BUY, callback_data="menu:payg")])
    prompt = texts.buy_prompt(
        session["price_per_gb"], session["min_gb"], session["max_gb"],
        session["plan_duration_days"],
    )
    if force_new:
        prompt = texts.NEW_SERVICE_INTRO + "\n\n" + prompt
    await update.effective_message.reply_text(prompt, reply_markup=InlineKeyboardMarkup(keyboard))


async def on_volume_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A volume tap (or «حجم دلخواه») on the inline volume screen.

    The screen the tap came from states its own intent (R1-2): the trailing
    `:f` on the callback means «سرویس جداگانه». When the state died (restart,
    stale screen) the tap re-seeds the flow FROM THE SCREEN — including that
    flag — instead of silently downgrading to a plain extend-their-service
    buy."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    arg = parts[1] if len(parts) > 1 else ""
    force = len(parts) > 2 and parts[2] == "f"
    if not context.user_data.get(_STATE) == _STATE_CHOOSING_VOLUME:
        # The state died (restart, stale screen) — the tap re-seeds it: the
        # screen they are looking at IS the volume screen, and what it offered
        # (plain buy vs سرویس جداگانه) is what the order must be.
        context.user_data[_STATE] = _STATE_CHOOSING_VOLUME
        context.user_data[_FORCE_NEW_SERVICE] = force
    if arg == "custom":
        await update.effective_message.reply_text(texts.ASK_CUSTOM_VOLUME)
        return
    try:
        volume = float(arg)
    except ValueError:
        return
    await _handle_volume(update, context, volume)


async def on_username_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«نام کاربری دلخواه» on the volume screen: the OPTIONAL pre-pick. The
    answer re-renders the volume screen; the name rides the order POST."""
    query = update.callback_query
    await query.answer()
    context.user_data[_STATE] = _STATE_AWAITING_PREFERRED_NAME
    await update.effective_message.reply_text(
        texts.ASK_PREFERRED_NAME,
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton(texts.SKIP_NAME)], [KeyboardButton(texts.CANCEL)]],
            resize_keyboard=True,
        ),
    )


async def on_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The buy screen's inline shortcuts to the menu-level flows (سرویس
    جداگانه / سرویس مصرفی): same handlers the reply-keyboard labels route
    to, so the two shapes cannot drift."""
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    context.user_data.clear()
    if action == "newservice":
        await start_new_service(update, context)
    elif action == "payg":
        await start_payg(update, context)


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


async def on_renew_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The renew button on ONE service card: ren:<account_id>:<gb>. Renews
    exactly that service with the same size - one tap to the summary. A
    malformed or stale payload is the usual «این دکمه قدیمی شده»."""
    query = update.callback_query
    await query.answer()
    try:
        _, raw_id, raw_gb = query.data.split(":", 2)
        account_id = int(raw_id)
        volume = float(raw_gb)
    except (AttributeError, ValueError):
        await update.effective_message.reply_text(texts.STALE_BUTTON)
        return

    context.user_data.clear()
    session = await _session(update)
    if session["is_blocked"]:
        await _reply(update, texts.blocked(_handle(session)), session)
        return
    if not session["is_open"]:
        await _reply(update, texts.shop_closed(_handle(session)), session)
        return
    if session.get("per_account_renewal"):
        context.user_data[_TARGET_ACCOUNT_ID] = account_id
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
        elif "can't be auto-renewed" in text or "no longer active" in text:
            # The backend's plain refusals (an operator-managed service, a
            # disabled one): information with the way forward, not a «technical
            # problem» — nothing is wrong, this service just does not do this.
            await _reply(update, texts.AUTO_RENEW_NOT_AVAILABLE, session)
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
    if intent.get("target_label"):
        head = texts.renew_target_line(intent["target_label"]) + "\n" + head
    if intent["balance"] > 0:
        head += (
            f"قیمت: {texts.money(intent['price'])}\n"
            f"از موجودی‌تان کم می‌شود: {texts.money(intent['balance'])}\n"
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
    """Volume chosen. Creates the ORDER — which takes no money — carrying the
    customer's pre-picked username when they chose one on the volume screen
    (T4: the ask is OFF the mandatory path; skip = the auto shop{id} name).
    Straight to the payment screen from here, whichever flavour applies."""
    session = await _session(update)
    if not (session["min_gb"] <= volume <= session["max_gb"]):
        await update.effective_message.reply_text(texts.out_of_range(session["min_gb"], session["max_gb"]))
        return

    payload = {
        "telegram_id": update.effective_user.id,
        "data_limit_gb": volume,
        "force_new_service": bool(context.user_data.get(_FORCE_NEW_SERVICE)),
    }
    chosen_name = context.user_data.get(_PENDING_PREFERRED_NAME)
    if chosen_name:
        payload["marzban_username"] = chosen_name
    target_id = context.user_data.get(_TARGET_ACCOUNT_ID)
    if target_id is not None and session.get("per_account_renewal"):
        payload["target_account_id"] = target_id
        payload["force_new_service"] = False

    try:
        intent = await backend.post("/api/shop/bot/orders", json=payload)
    except ShopApiError as exc:
        if target_id is not None and (getattr(exc, "status", 0) == 404 or "no longer active" in str(exc)):
            # The tapped card is gone / not theirs any more: no order was
            # created, nothing moved. Say so and point back at the list.
            context.user_data.clear()
            await _reply(update, texts.RENEW_TARGET_GONE, session)
            return
        if getattr(exc, "status", 0) == 409 and "SERVICE_IS_UNLIMITED" in str(exc):
            # Their current service has no cap — a volume purchase has nothing
            # to add to it, and the backend refused before any money moved.
            # This is a "you don't need this", not an error.
            context.user_data.clear()
            await _reply(update, texts.service_unlimited(_handle(session)), session)
            return
        if getattr(exc, "status", 0) == 409 and "PAYG_TOPUP_INSTEAD" in str(exc):
            # F3(b): their current service is METERED — the backend refuses a
            # fixed-volume package on it (before any money moves) and wants a
            # wallet top-up instead. A "buy the other way", not an error.
            context.user_data.clear()
            await _reply(update, texts.PAYG_TOPUP_INSTEAD, session)
            return
        if getattr(exc, "status", 0) == 409 and "قبلاً گرفته شده" in str(exc):
            # The pre-picked username is taken (live availability check ran
            # server-side). The backend's sentence already says "pick another"
            # — reopen the ask so they can.
            context.user_data.pop(_PENDING_PREFERRED_NAME, None)
            context.user_data[_STATE] = _STATE_AWAITING_PREFERRED_NAME
            await update.effective_message.reply_text(str(exc))
            await update.effective_message.reply_text(
                texts.ASK_PREFERRED_NAME,
                reply_markup=ReplyKeyboardMarkup(
                    [[KeyboardButton(texts.SKIP_NAME)], [KeyboardButton(texts.CANCEL)]],
                    resize_keyboard=True,
                ),
            )
            return
        logger.exception("Could not create an order for %s", update.effective_user.id)
        context.user_data.clear()
        await _reply(update, texts.generic_error(_handle(session)), session)
        return

    context.user_data[_ORDER_ID] = intent["order_id"]
    context.user_data[_PENDING_VOLUME] = intent["data_limit_gb"]
    context.user_data.pop(_PENDING_PREFERRED_NAME, None)  # consumed

    return await _payment_screens(update, context, intent)


def _cancel_keyboard(session: dict | None) -> ReplyKeyboardMarkup:
    """[انصراف] with the main-menu rows UNDER it (R3, screen-transcript read).

    The receipt wait is the longest, most anxious wait in the product — and
    the three prompts that open it used to replace the whole reply keyboard
    with a single ❌ انصراف button, hiding the menu at the exact moment the
    customer is most likely to want out (or their «حساب من»). Menu taps were
    ALWAYS routed during this state (handle_text checks the menu first) —
    now they are visible too, the same fix T7 applied to the phone prompt."""
    rows = [[KeyboardButton(texts.CANCEL)]]
    rows += [[KeyboardButton(b.text) for b in row] for row in main_menu(session).keyboard]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def _payment_screens(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           intent: dict) -> None:
    """The two payment screens (one-tap wallet confirm / card-to-card receipt
    ask), driven by the order intent. The confirm carries ONE button —
    «تأیید و دریافت» — as an INLINE keyboard, so the main menu on the reply
    keyboard stays visible underneath; the old typed «تأیید» path keeps
    working for reply keyboards still sitting in old chats."""
    session = await _session(update)

    if intent["payable_from_wallet"]:
        # Enough credit already: one tap, no card, no human in the loop.
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        context.user_data[_STATE] = _STATE_CONFIRMING_WALLET_BUY
        confirm_text = texts.confirm_from_wallet(
            intent["data_limit_gb"], intent["duration_days"],
            intent["price"], intent["balance"],
        )
        if intent.get("target_label"):
            confirm_text = texts.renew_target_line(intent["target_label"]) + "\n" + confirm_text
        await update.effective_message.reply_text(
            confirm_text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(texts.CONFIRM_BUY, callback_data="buy:confirm")],
                [InlineKeyboardButton(texts.CANCEL, callback_data="buy:cancel")],
            ]),
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
        reply_markup=_cancel_keyboard(session),
    )


async def on_buy_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The inline confirm/cancel under the wallet-purchase summary (T4). A
    STALE confirm — the flow already paid, or died with a restart — gets an
    answer, never silence."""
    query = update.callback_query
    await query.answer()
    if query.data == "buy:cancel":
        had_flow = context.user_data.get(_STATE) is not None
        context.user_data.clear()
        session = None
        try:
            session = await _session(update)
        except ShopApiError:
            pass
        await query.edit_message_text(
            texts.CANCELLED if had_flow else texts.STALE_BUTTON,
            reply_markup=None,
        )
        return
    # buy:confirm
    if context.user_data.get(_STATE) != _STATE_CONFIRMING_WALLET_BUY or not context.user_data.get(_ORDER_ID):
        # reply_markup=None so the dead تأیید/انصراف pair comes off the message
        # with the answer (R1-7) — same as the cancel branch above.
        await query.edit_message_text(texts.STALE_BUTTON, reply_markup=None)
        return
    await _confirm_wallet_purchase(update, context)


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
            reply_markup=_cancel_keyboard(session),
        )
        return
    await update.effective_message.reply_text(
        texts.topup_instructions(
            value, session["card_number"], session.get("card_holder"),
            session.get("approval_eta_minutes", 30),
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_cancel_keyboard(session),
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
        lines.append(f"معمولاً تا {texts.fa(eta)} دقیقه بررسی و موجودی‌تان خودکار شارژ می‌شود.")
    lines.append("لازم نیست ربات را باز نگه دارید — خبرش همین‌جا می‌آید.")
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
    someone who has just sent money.

    (R1-1, review 2026-11-06: T7 briefly put the free-text money-words branch
    here too — reading an undefined `text`, so every voice note crashed with
    NameError into on_error's scary message. Money words can only arrive as
    TEXT, and handle_text's tail answers them; a non-text message has none.)"""
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


_QUESTION_WORDS = ("کی ", "چی ", "چرا", "چقدر", "چند ", "میشه", "می‌شه", "سلام",
                   "لطفا", "می‌خوام")


def _looks_like_question(text: str) -> bool:
    """A real question typed while the bot waits for a receipt — the customer
    deserves an answer (the nudge + menu), not a form rejection. A receipt /
    tracking code is digits-heavy, so the two classifiers barely overlap."""
    if "؟" in text or "?" in text:
        return True
    lowered = text.strip()
    return any(w in lowered for w in _QUESTION_WORDS)


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


async def show_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The ONE money door (T2): debt flavour when the customer owes, wallet
    flavour otherwise — never both numbers side by side.

    The debt view's buttons carry their amounts: «پرداخت از موجودی (X)»
    pays on the tap (the money goes to the customer's own account, and the
    amount they are paying was the button they read), «ارسال رسید کارت» opens
    the receipt flow with the whole debt one button away. The per-account
    breakdown stays behind «جزئیات»."""
    context.user_data.clear()
    try:
        session = await _session(update)
        history = await backend.get("/api/shop/bot/wallet", params={"telegram_id": update.effective_user.id})
    except ShopApiError:
        logger.exception("Account view failed for %s", update.effective_user.id)
        await update.effective_message.reply_text(texts.generic_error(None))
        return

    debt = session.get("ledger_debt", 0)
    if debt > 0:
        await _send_debt_view(update, session, debt)
        return

    if not session["balance"] and not history:
        await _reply(update, texts.WALLET_EMPTY, session)
        return

    recent = [f"{_wallet_label(entry)}: {texts.money(abs(entry['amount']))}" for entry in history[:3]]
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    await update.effective_message.reply_text(
        texts.account_wallet_view(session["balance"], recent),
        # The top-up action rides an inline button so it never clobbers the
        # main menu on the reply keyboard underneath.
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ شارژ", callback_data="acct:topup"),
        ]]),
    )


async def on_account_topup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«➕ شارژ» on the wallet flavour of «حساب من»: the top-up amount ask."""
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await start_topup(update, context)


async def _send_debt_view(update: Update, session: dict, debt: float) -> None:
    """The owing side of «حساب من». One number; the actions sit in the inline
    keyboard. The main menu STAYS on the reply keyboard underneath — the way
    back is always visible."""
    from telegram import InlineKeyboardMarkup
    await update.effective_message.reply_text(
        texts.account_debt_view(debt, session.get("debt_exact", True)),
        reply_markup=InlineKeyboardMarkup(_debt_buttons(session, debt)),
    )


async def on_account_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«جزئیات»: the per-account split of the debt (where it comes from) —
    information only, reachable by request, never the default screen."""
    query = update.callback_query
    await query.answer()
    session = await _session(update)
    debt = session.get("ledger_debt", 0)
    if debt <= 0:
        await query.edit_message_text(texts.DEBT_NONE)
        return
    detail = texts.debt_status(
        debt, session.get("debt_breakdown"), session.get("debt_exact", True),
    )
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    await query.edit_message_text(
        detail,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("بازگشت", callback_data="acct:back"),
        ]]),
    )


async def on_account_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Back from «جزئیات» to the debt view."""
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    session = await _session(update)
    debt = session.get("ledger_debt", 0)
    if debt <= 0:
        await query.edit_message_text(texts.DEBT_NONE)
        return
    from telegram import InlineKeyboardMarkup
    await query.edit_message_text(
        texts.account_debt_view(debt, session.get("debt_exact", True)),
        reply_markup=InlineKeyboardMarkup(_debt_buttons(session, debt)),
    )


def _debt_buttons(session: dict, debt: float) -> list:
    """The debt view's inline keyboard: the wallet-pay button (with its
    amount, when the wallet holds anything), the card-receipt route, and
    «جزئیات» only when the backend actually sent a breakdown."""
    from telegram import InlineKeyboardButton

    buttons: list[list[InlineKeyboardButton]] = []
    if session["balance"] > 0:
        pay = min(session["balance"], int(debt))
        buttons.append([InlineKeyboardButton(
            text=f"💳 پرداخت از موجودی ({texts.money(pay)})",
            callback_data="debt:wallet",
        )])
    buttons.append([InlineKeyboardButton(text=texts.DEBT_PAY_BY_RECEIPT, callback_data="debt:receipt")])
    if session.get("debt_breakdown"):
        buttons.append([InlineKeyboardButton("جزئیات", callback_data="acct:detail")])
    return buttons


async def start_pay_debt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Legacy entry (old chats' «💳 پرداخت بدهی صورت‌حساب» button and the
    bot.py import): now the merged account view — the debt flavour when they
    owe, the wallet flavour otherwise."""
    await show_account(update, context)


async def _start_debt_receipt(update, context, session, debt) -> None:
    """The card-receipt debt flow: the WHOLE debt is one button away, and a
    smaller typed amount still works."""
    context.user_data[_STATE] = _STATE_CHOOSING_TOPUP
    context.user_data[_PENDING_FOR_DEBT] = True
    await update.effective_message.reply_text(
        texts.debt_receipt_amount_ask(int(debt), session["max_topup"]),
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton(texts.DEBT_AMOUNT_AS_IS)], [KeyboardButton(texts.CANCEL)]],
            resize_keyboard=True,
        ),
    )


async def on_pay_debt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The debt view's two entries (T2 one-tap design).

    debt:wallet pays ON the tap — the button carried the amount, the money
    goes to the customer's own account, and the screen they tapped it on was
    already the payment summary. The OLD two-tap confirm (debt:wallet:yes,
    still sitting in old chats) routes to the same immediate payment.
    Menu buttons cancel any ask and run their own flow — the same resilience
    rule as the name steps."""
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    if query.data == "debt:wallet":
        await _pay_debt_from_wallet(update, query)
        return
    if query.data == "debt:cancel":
        # The OLD keyboard's cancel button: this callback was registered but
        # never routed — a cancel tap used to OPEN the receipt flow (found in
        # the B-3 review). Answer the tap, put the debt view back.
        session = await _session(update)
        debt = session.get("ledger_debt", 0)
        if debt <= 0:
            await query.edit_message_text(texts.DEBT_NONE)
            return
        from telegram import InlineKeyboardMarkup
        await query.edit_message_text(
            texts.account_debt_view(debt, session.get("debt_exact", True)),
            reply_markup=InlineKeyboardMarkup(_debt_buttons(session, debt)),
        )
        return
    # debt:receipt
    session = await _session(update)
    debt = session.get("ledger_debt", 0)
    if debt <= 0:
        await query.edit_message_text(texts.DEBT_NONE)
        return
    await _start_debt_receipt(update, context, session, debt)


async def _pay_debt_from_wallet(update: Update, query) -> None:
    """The wallet debt payment itself — shared by the one-tap button and the
    legacy confirm button (old chats)."""
    session = await _session(update)
    if session.get("ledger_debt", 0) <= 0 or session["balance"] <= 0:
        await query.edit_message_text(
            texts.DEBT_NONE if session.get("ledger_debt", 0) <= 0
            else texts.account_debt_view(session.get("ledger_debt", 0), session.get("debt_exact", True))
        )
        return
    try:
        result = await backend.post("/api/shop/bot/pay-debt-from-wallet",
                                    json={"telegram_id": update.effective_user.id}, timeout=60)
    except ShopApiError as exc:
        # ShopApiError's str IS the backend detail — the backend writes these
        # two refusals in customer-facing Persian (see routers.shop); anything
        # unexpected falls back to the generic sentence.
        detail = str(exc) or ""
        await query.edit_message_text(detail if detail else texts.generic_error(_handle(session)))
        return
    await query.edit_message_text(
        texts.debt_paid_from_wallet(result["paid"], result["wallet_balance"], result["ledger_debt"])
    )


async def on_pay_debt_wallet_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The legacy wallet-pay confirm button, still sitting in chats from
    before the rebuild: same immediate payment as the new one-tap button."""
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await _pay_debt_from_wallet(update, query)


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
    """Legacy entry (the old wallet button still in some chats): the merged account view.
    The wallet flavour shows the balance; if the customer also owes, the debt
    flavour wins (ONE number per view)."""
    await show_account(update, context)


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
        # T3: an ended card states what happened AND what one tap does, and
        # its تمدید button becomes the FIRST thing on the card.
        ended = (limit_gb is not None and used_gb >= limit_gb) or (days_left is not None and days_left <= 0)
        # A renewal extends ONE service (the backend's renewable_account), not
        # the card it was tapped on. The backend marks that card; every other
        # card of a multi-service customer carries no renew button, so no
        # tap can extend (and charge for) the wrong service. A backend that
        # predates the field is treated as before (button shown).
        per_account = bool(session.get("per_account_renewal")) and row.get("can_renew") is not None
        if per_account:
            # Every card that can take a volume purchase carries ITS OWN renew
            # button, and the button names this service.
            can_renew = bool(row.get("can_renew"))
        else:
            can_renew = row.get("renews_here", True)
        if ended and can_renew:
            lines.append(texts.RENEW_HINT_ENDED)
        # The card carries its own actions (T3): تمدید starts the renewal of
        # the same size via the EXISTING renew: callback (the backend's expiry
        # warnings use the same path — renewal-in-place). لینک اشتراک is a URL
        # BUTTON, not the raw link: subscription URLs churn on every
        # panel-side modify (live 2026-10-01) and a Latin URL inside Persian
        # text gets bidi-reordered. The rarer actions (the nickname,
        # auto-renew) sit one level down behind ⋯ بیشتر.
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        buttons = []
        if can_renew and limit_gb is not None and row.get("account_id"):
            renew_cb = (f"ren:{row['account_id']}:{round(limit_gb, 2):g}" if per_account
                        else f"renew:{round(limit_gb, 2):g}")
            buttons.append([InlineKeyboardButton(texts.CARD_RENEW_BUTTON, callback_data=renew_cb)])
        if row["subscription_url"]:
            buttons.append([InlineKeyboardButton("🔗 لینک اشتراک", url=row["subscription_url"])])
        if row.get("account_id"):
            buttons.append([InlineKeyboardButton(texts.CARD_MORE_BUTTON,
                                                 callback_data=f"svcmore:{row['account_id']}")])
        keyboard = InlineKeyboardMarkup(buttons) if buttons else None
        await update.effective_message.reply_text("\n".join(lines), reply_markup=keyboard)

    # (No wallet trailer: money has its own door — «حساب من» — and mixing the
    # two on the services screen is the T0 finding this rebuild removes.)


async def on_service_more(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«⋯ بیشتر» on a service card: the rarer per-service actions, one level
    down so the card itself stays narrow. Ownership for both actions is
    re-checked server-side when they actually run."""
    query = update.callback_query
    await query.answer()
    try:
        account_id = query.data.split(":", 1)[1]
        if not account_id.isdigit():
            raise ValueError(account_id)
    except (IndexError, ValueError):
        await update.effective_message.reply_text(texts.STALE_BUTTON)
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    buttons = [[InlineKeyboardButton("✏️ نام سرویس", callback_data=f"svcname:{account_id}")]]
    # Auto-renew debits the SHOP wallet, so the backend refuses it for a
    # service the operator manages («linked» rows). Offer it only where it can
    # work. If the list cannot be fetched the button stays (the backend and
    # the friendly refusal line still guard it).
    offer_auto_renew = True
    try:
        rows = await backend.get("/api/shop/bot/accounts",
                                 params={"telegram_id": update.effective_user.id})
        row = next((r for r in rows if str(r.get("account_id")) == account_id), None)
        if row is not None:
            ok = row.get("auto_renew_ok")
            # The backend's own verdict wins; an older backend (no field)
            # falls back to the shop/linked source guess.
            offer_auto_renew = bool(ok) if ok is not None else row.get("source") == "shop"
    except ShopApiError:
        logger.debug("Could not check the service source for the more-panel", exc_info=True)
    if offer_auto_renew:
        buttons.append([InlineKeyboardButton(texts.AUTO_RENEW_BUTTON,
                                             callback_data=f"autorenew:{account_id}")])
    await update.effective_message.reply_text(
        texts.SERVICE_MORE_HINT,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def on_service_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The ✏️ نام button on a service: asks for the customer's own nickname.
    Ownership is validated SERVER-side when the name is saved (the callback
    only opens the ask); the id rides in the callback data."""
    query = update.callback_query
    await query.answer()
    try:
        account_id = query.data.split(":", 1)[1]
        if not account_id.isdigit():
            raise ValueError(account_id)
    except (IndexError, ValueError):
        await update.effective_message.reply_text(texts.STALE_BUTTON)
        return
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

    # The OPTIONAL username pre-pick (T4) answers BEFORE the menu dispatch —
    # named escapes only: «انصراف» returns to the volume screen, a menu button
    # cancels AND runs its menu, «بدون نام» clears the pick, anything else is
    # the chosen username.
    if context.user_data.get(_STATE) == _STATE_AWAITING_PREFERRED_NAME:
        if text == texts.CANCEL:
            context.user_data.pop(_PENDING_PREFERRED_NAME, None)
            context.user_data[_STATE] = _STATE_CHOOSING_VOLUME
            session = await _session(update)
            await _render_volume_screen(update, session,
                                        force_new=bool(context.user_data.get(_FORCE_NEW_SERVICE)))
            return
        if _is_menu_tap(text):
            context.user_data.clear()
            return await handle_text(update, context)  # re-dispatch as a menu tap
        if text == texts.SKIP_NAME:
            context.user_data.pop(_PENDING_PREFERRED_NAME, None)  # auto shop{id}
        else:
            chosen = text.strip()
            # Instant local pattern check; the definitive availability check
            # runs server-side at order creation, and AGAIN at create_user (a
            # race refunds, never double-charges).
            if not re.match(r"^[a-zA-Z0-9_-]{3,30}$", chosen):
                await update.effective_message.reply_text(texts.USERNAME_INVALID)
                return
            context.user_data[_PENDING_PREFERRED_NAME] = chosen
        context.user_data[_STATE] = _STATE_CHOOSING_VOLUME
        session = await _session(update)
        await _render_volume_screen(update, session,
                                    force_new=bool(context.user_data.get(_FORCE_NEW_SERVICE)))
        return

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
        if name == texts.CANCEL or _is_menu_tap(name):
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

    # (The old _STATE_AWAITING_OPTIONAL_NAME branch — whose order builder no
    # longer even exists — went with the T4 buy-flow rewrite; nothing sets
    # that state since F-E2 replaced the flow.)

    if text == texts.PHONE_LATER:
        context.user_data.clear()
        session = await _session(update)
        await _reply(update, texts.PHONE_LATER_ACK, session)
        return

    if text in (texts.MENU_BUY, texts.MENU_BUY_RENEW):
        return await start_buy(update, context)
    if text in (texts.MENU_NEW_SERVICE, texts.NEW_SERVICE_ON_BUY):
        return await start_new_service(update, context)
    if text in (texts.MENU_PAYG, texts.PAYG_ON_BUY):
        return await start_payg(update, context)
    if text == texts.MENU_TRIAL:
        return await take_trial(update, context)
    if text == texts.MENU_ACCOUNTS:
        return await show_accounts(update, context)
    if text in (texts.MENU_WALLET, texts.MENU_PAY_DEBT) or texts.is_menu_account_label(text):
        return await show_account(update, context)
    if text == texts.MENU_TOPUP:
        return await start_topup(update, context)
    if text in (texts.MENU_HELP, texts.MENU_SUPPORT, texts.MENU_HELP_SUPPORT):
        return await show_help_support(update, context)

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
        # «همین مبلغ» = the whole debt, no typing (T2). Checked BEFORE the
        # number parser, which would reject the label.
        if text == texts.DEBT_AMOUNT_AS_IS:
            session = await _session(update)
            debt = session.get("ledger_debt", 0)
            if debt <= 0:
                context.user_data.clear()
                await _reply(update, texts.DEBT_NONE, session)
                return
            return await _handle_topup_amount(update, context, debt)
        amount = parse_number(text)
        if amount is None:
            await update.effective_message.reply_text(texts.not_a_number("۲۰۰٬۰۰۰"))
            return
        return await _handle_topup_amount(update, context, amount)

    if state == _STATE_AWAITING_RECEIPT:
        # A plausible tracking code is accepted as the receipt itself — see
        # _submit_receipt. A QUESTION («کی تایید می‌کنی؟») is answered, not
        # bounced with the receipt prompt (T7); anything else non-receipt
        # gets the prompt, still without opening a pending top-up: that would
        # burn the operator's attention on something that isn't proof.
        if _looks_like_receipt_text(text):
            return await _submit_receipt(update, context, receipt_text=text.strip())
        if _looks_like_question(text):
            session = await _session(update)
            await _reply(update, texts.not_understood(_handle(session)), session)
            return
        await update.effective_message.reply_text(texts.NEED_PHOTO)
        return

    # R1-16 (review 2026-11-06): «همین مبلغ» is a FLOW button — a tap with no
    # flow state behind it (stale keyboard, restart) still means "pay my
    # debt": open the debt door instead of saying «متوجه نشدم».
    if text == texts.DEBT_AMOUNT_AS_IS:
        return await show_account(update, context)

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

    # Free-text money words get the answer they are asking for (T7): the
    # balance AND the debt live behind one door — «حساب من».
    if any(word in text for word in ("موجودی", "حساب", "بدهی", "بدهکار", "باقیمانده")):
        return await show_account(update, context)

    await _reply(update, texts.not_understood(_handle(session)), session)
