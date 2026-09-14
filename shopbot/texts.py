"""Every string the BOT says in reply to a tap.

The other half of the customer-facing copy lives in `backend/app/shop_texts.py`
— what the BACKEND pushes when there is no open request to reply to (a
delivery, an operator's approval landing hours later, an expiry warning). The
split is by sender, not by topic, and delivery wording lives on the backend
side because it fires from two triggers only the backend can see.

VOCABULARY — settled once, because the same object under four names is how a
customer ends up hunting the menu for a button that is right in front of them:

  سرویس        the thing being sold. NOT «اشتراک» — in Persian that reads
               first as "sharing", and this market says «سرویس» or «کانفیگ».
               The one exception is «لینک اشتراک», which is the fixed term
               every v2rayNG user already knows.
  کیف پول      the stored balance (the object)
  شارژ         putting money into it (the verb)
  موجودی       the number inside it
  «-تان»       not «شما». Polite, but the way a shopkeeper talks rather than
               the way a bank writes.

NUMBERS: Persian digits everywhere, EXCEPT an amount or card number the
customer has to retype into a banking app. A Persian-digit amount pasted into
a bank app is a failed transfer and then a support conversation.

NO RAW BACKEND TEXT REACHES A CUSTOMER. The backend's error messages are
written for the operator and are in English; for a 422 they can be a Python
list. This module owns every sentence the customer reads, and anything
unmapped falls back to generic_error() rather than printing an exception.
"""

from __future__ import annotations

from typing import Optional

_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa(value) -> str:
    """Persian digits with separators — for anything the customer only reads."""
    if isinstance(value, float) and value != int(value):
        text = f"{value:,.2f}".rstrip("0").rstrip(".")
    else:
        text = f"{int(value):,}"
    return text.translate(_PERSIAN_DIGITS)


def money(amount: int) -> str:
    return f"{fa(amount)} تومان"


def money_to_type(amount: int) -> str:
    """Latin digits: this one gets typed into a bank app."""
    return f"{amount:,} تومان"


def gb(value: float) -> str:
    return f"{fa(value)} گیگ"


def support(handle: Optional[str]) -> str:
    """Empty when no handle is set, so a caller can always append it.

    An instruction the customer cannot follow is worse than no instruction —
    the previous copy told people to "contact support" four times without ever
    naming anyone to contact.
    """
    return f"\n\nسؤالی داشتید: @{handle.lstrip('@')}" if handle else ""


# ── menu ──────────────────────────────────────────────────────────────────

MENU_BUY = "🛒 خرید سرویس"
MENU_TRIAL = "🎁 تست رایگان"
MENU_ACCOUNTS = "📱 سرویس‌های من"
MENU_WALLET = "💰 کیف پول"
MENU_TOPUP = "➕ شارژ کیف پول"
MENU_HELP = "❓ راهنما"
MENU_SUPPORT = "💬 پشتیبانی"

CONFIRM_BUY = "✅ تأیید و دریافت"
CANCEL = "❌ انصراف"


def welcome(shop_name: Optional[str], trial_available: bool, trial_gb: float, trial_hours: int) -> str:
    """First screen. Leads with the trial when there is one, because it is the
    only thing here that asks nothing at all of a stranger."""
    name = shop_name or "فروشگاه"
    head = f"سلام 👋\nبه {name} خوش آمدید."
    if trial_available:
        return (
            f"{head}\n\n"
            f"اگر اولین بارتان است، «{MENU_TRIAL}» را بزنید — "
            f"{gb(trial_gb)} برای {fa(trial_hours)} ساعت، رایگان و بدون پرداخت.\n"
            "اول امتحان کنید، بعد تصمیم بگیرید."
        )
    return f"{head}\n\nاز منوی پایین شروع کنید."


def help_text(price_per_gb: int, days: int, eta_minutes: int, handle: Optional[str]) -> str:
    return (
        "کار با ربات ساده است:\n\n"
        f"۱. «{MENU_BUY}» را بزنید و حجم را انتخاب کنید.\n"
        "۲. مبلغ دقیق و شماره کارت را می‌بینید — کارت‌به‌کارت کنید و عکس رسید را بفرستید.\n"
        f"۳. رسید را که ببینم (معمولاً تا {fa(eta_minutes)} دقیقه)، سرویس‌تان خودکار ساخته می‌شود "
        "و لینک و کد QR همینجا می‌آید.\n\n"
        f"قیمت هر گیگابایت {money(price_per_gb)} و همه‌ی سرویس‌ها {fa(days)} روزه‌اند؛ فقط حجمشان فرق می‌کند.\n"
        "اگر از قبل کیف پولتان شارژ باشد، خرید همان لحظه انجام می‌شود."
        + support(handle)
    )


# ── buying ────────────────────────────────────────────────────────────────

def buy_prompt(price_per_gb: int, min_gb: float, max_gb: float, days: int) -> str:
    return (
        f"هر گیگابایت {money(price_per_gb)} — همه‌ی سرویس‌ها {fa(days)} روزه\n"
        f"از {gb(min_gb)} تا {gb(max_gb)}\n\n"
        "یکی را انتخاب کنید، یا عدد حجم دلخواهتان را بفرستید (مثلاً ۳۵)."
    )


def confirm_from_wallet(volume: float, days: int, price: int, balance: int) -> str:
    """The repeat-customer path: enough credit, one tap, no card transfer.

    Deliberately no "balance after purchase" line. The previous flow printed
    one, which for a first-time buyer rendered a NEGATIVE number — and in
    right-to-left text the minus sign detaches from its digits, so it read as
    debt owed to the shop.
    """
    return (
        f"{gb(volume)} — {fa(days)} روزه\n"
        f"قیمت: {money(price)}\n"
        f"موجودی کیف پولتان: {money(balance)}\n\n"
        "از کیف پولتان کم می‌شود و سرویس همین الان ساخته می‌شود."
    )


BUY_WORKING = "دارم سرویس‌تان را می‌سازم… چند لحظه."


def not_a_number(example: str) -> str:
    return f"لطفاً فقط عدد بفرستید، مثلاً {example}."


def out_of_range(min_gb: float, max_gb: float) -> str:
    return f"حجم باید بین {gb(min_gb)} و {gb(max_gb)} باشد."


# ── the trial ─────────────────────────────────────────────────────────────

def trial_offer(trial_gb_value: float, trial_hours: int) -> str:
    return (
        f"🎁 {gb(trial_gb_value)} برای {fa(trial_hours)} ساعت، رایگان.\n"
        "بدون پرداخت و بدون کارت — فقط برای اینکه ببینید کار می‌کند.\n\n"
        "بزنید تا همین الان بسازمش."
    )


TRIAL_ALREADY_TAKEN = (
    "تست رایگانتان را قبلاً گرفته‌اید 🙂\n"
    f"برای ادامه از «{MENU_BUY}» یک سرویس بگیرید."
)
TRIAL_UNAVAILABLE = "تست رایگان فعلاً فعال نیست."


# ── paying ────────────────────────────────────────────────────────────────

ASK_RECEIPT = "حالا عکس رسید را بفرستید 📸"

NEED_PHOTO = (
    "رسید را به صورت عکس بفرستید، نه متن.\n"
    "اگر رسیدتان PDF است، از آن عکس بگیرید."
)

RECEIPT_WITHOUT_CONTEXT = (
    "این عکس مربوط به چه پرداختی است؟\n"
    f"اول از «{MENU_BUY}» سرویس را انتخاب کنید یا «{MENU_TOPUP}» را بزنید، بعد رسید را بفرستید."
)


def topup_ask_amount(min_topup: int, max_topup: int) -> str:
    return (
        "چه مبلغی می‌خواهید شارژ کنید؟ عدد را به تومان بفرستید (مثلاً ۲۰۰,۰۰۰).\n"
        f"از {money(min_topup)} تا {money(max_topup)}."
    )


def topup_out_of_range(min_topup: int, max_topup: int) -> str:
    return f"مبلغ شارژ باید بین {money(min_topup)} و {money(max_topup)} باشد. یک مبلغ دیگر بفرستید."


def topup_instructions(amount: int, card_number: str, card_holder: Optional[str], eta_minutes: int) -> str:
    holder = f"\nبه نام: {card_holder}" if card_holder else ""
    return (
        f"مبلغ {money_to_type(amount)} را به این کارت واریز کنید:\n\n"
        f"`{card_number}`{holder}\n\n"
        f"بعد عکس رسید را همینجا بفرستید.\n"
        f"معمولاً تا {fa(eta_minutes)} دقیقه بررسی می‌شود."
    )


NO_CARD = "هنوز شماره کارت ثبت نکرده‌ام. یک لحظه پیام بدهید تا درستش کنم."


# ── wallet & accounts ─────────────────────────────────────────────────────

def wallet_summary(balance: int) -> str:
    return f"💰 موجودی کیف پولتان: {money(balance)}"


WALLET_EMPTY = (
    "کیف پولتان خالی است.\n"
    f"برای خرید لازم نیست از قبل شارژ کنید — کافی است «{MENU_BUY}» را بزنید."
)

NO_ACCOUNTS = f"هنوز سرویسی نگرفته‌اید. از «{MENU_BUY}» شروع کنید."


def account_line(volume_gb: Optional[float], used_gb: float, days_left: Optional[int]) -> str:
    """One subscription, named by what it IS rather than by its panel username.

    The previous version printed the raw Marzban username — a Latin identifier
    the customer cannot use, cannot read, and which leaked the panel
    software's name into a consumer screen.
    """
    if days_left is None:
        life = "بدون انقضا"
    elif days_left <= 0:
        life = "تمام شده"
    else:
        life = f"{fa(days_left)} روز مانده"
    used = fa(round(used_gb, 2))
    if volume_gb is None:
        return f"🔑 سرویس نامحدود\nمصرف: {used} گیگ · {life}"
    return f"🔑 سرویس {gb(volume_gb)}\nمصرف: {used} از {fa(volume_gb)} گیگ · {life}"


# ── outcomes & errors ─────────────────────────────────────────────────────

def shop_closed(handle: Optional[str]) -> str:
    return "فعلاً فروش بسته است. کمی بعد دوباره سر بزنید 🙏" + support(handle)


def blocked(handle: Optional[str]) -> str:
    return (
        "با این حساب فعلاً نمی‌توانید خرید کنید.\n"
        "اگر فکر می‌کنید اشتباه شده، پیام بدهید تا بررسی کنم." + support(handle)
    )


def generic_error(handle: Optional[str]) -> str:
    """The catch-all. Never the backend's own words: those are English, written
    for the operator, and for a validation failure they can be a Python list."""
    return (
        "یک مشکل فنی پیش آمد و کاری انجام نشد.\n"
        "چند لحظه بعد دوباره بزنید." + support(handle)
    )


def delivered_but_no_qr(handle: Optional[str]) -> str:
    """Sent after the money has already moved. Leads with the reassurance,
    because the first thing the eye lands on decides whether this reads as
    "my service is ready" or "I lost my money"."""
    return (
        "✅ سرویس‌تان ساخته شد.\n"
        f"فقط ارسال کد QR گیر کرد — لینک را از «{MENU_ACCOUNTS}» بردارید." + support(handle)
    )


CANCELLED = "باشد، لغو شد. هر وقت خواستید از منو ادامه بدهید."


def not_understood(handle: Optional[str]) -> str:
    """Replaces bouncing every unrecognised message back to the welcome screen.

    Someone typing «کی تاییدش میکنی؟» has a real and urgent question;
    answering it with a greeting reads as being ignored by a machine at
    exactly the moment they most need a person.
    """
    base = "متوجه نشدم 🙏\nاز دکمه‌های پایین استفاده کنید."
    return base + (support(handle) or "")


def support_text(handle: Optional[str]) -> str:
    if handle:
        return f"هر سؤال یا مشکلی داشتید به @{handle.lstrip('@')} پیام بدهید — خودم جواب می‌دهم."
    return "فعلاً راه ارتباطی ثبت نشده. کمی بعد دوباره امتحان کنید."
