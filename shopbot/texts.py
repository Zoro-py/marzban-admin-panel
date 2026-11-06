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
  موجودی       the stored balance — the ONE number (T5). «شارژ» is the
               verb for putting money into it.
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
    """Persian digits with separators — for anything the customer only reads.
    Decimals use the Persian separator «٫», not «.», which in a Persian line
    reads as a stray full stop."""
    if isinstance(value, float) and value != int(value):
        text = f"{value:,.2f}".rstrip("0").rstrip(".").replace(".", "٫")
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

# Layout A (T2, docs/ZCODE_SHOPBOT_UX_DESIGN.md): at most four top-level
# doors. The old separate wallet / top-up / pay-debt / new-service / payg /
# help / support labels below are LEGACY — old chats still show those
# keyboards, so every one of them keeps routing to its action in
# handlers/shop.py, but no new menu renders them.

MENU_BUY = "🛒 خرید سرویس"
# Shown INSTEAD of MENU_BUY to a customer who already has services: the same
# action IS their renewal, and the label should say so.
MENU_BUY_RENEW = "🛒 خرید / تمدید سرویس"
# Legacy shape of MENU_BUY (kept so an old chat's keyboard still routes).
MENU_NEW_SERVICE = "✨ ساخت سرویس جدید (جدا از فعلی)"
# Legacy top-level label; the button now lives on the buy screen.
MENU_PAYG = "📊 سرویس مصرفی"
MENU_TRIAL = "🎁 تست رایگان"
MENU_ACCOUNTS = "📱 سرویس‌های من"
MENU_WALLET = "💰 کیف پول"
MENU_TOPUP = "➕ شارژ کیف پول"
MENU_HELP = "❓ راهنما"
MENU_SUPPORT = "💬 پشتیبانی"
MENU_PAY_DEBT = "💳 پرداخت بدهی صورت‌حساب"

# The ONE money door. When the customer owes, the debt itself is the label —
# the answer to "what do I do now" is visible without opening anything.
MENU_ACCOUNT = "💳 حساب من"
# Prefix of the owing variant's label (the amount follows it) — the
# dispatcher routes anything that starts with this to the same screen.
MENU_ACCOUNT_DEBT_PREFIX = "💳 ماندهٔ پرداخت:"


def menu_account_label(debt: float) -> str:
    if debt > 0:
        return f"{MENU_ACCOUNT_DEBT_PREFIX} {money(int(debt))}"
    return MENU_ACCOUNT


def is_menu_account_label(text: str) -> bool:
    """True for BOTH shapes of the money door's button (the plain one and the
    debt-carrying one) — used by the dispatchers."""
    return text == MENU_ACCOUNT or text.startswith(MENU_ACCOUNT_DEBT_PREFIX)


MENU_HELP_SUPPORT = "❓ راهنما و پشتیبانی"

# Conditional buttons on the BUY screen (not top-level menu items): a second,
# independent service, and the metered pay-as-you-go start. Each appears only
# when it is meaningful for this customer (see start_buy).
NEW_SERVICE_ON_BUY = "✨ سرویس جداگانه (برای دستگاه دیگر)"
PAYG_ON_BUY = "📊 سرویس مصرفی"

# First button of the debt-receipt amount ask: pays the WHOLE debt without
# typing the number.
DEBT_AMOUNT_AS_IS = "همین مبلغ"

CONFIRM_BUY = "✅ تأیید و دریافت"
# The common sizes are buttons; this is the way out of them for someone who
# wants a size that is not offered.
CUSTOM_VOLUME = "✏️ حجم دلخواه"
ASK_CUSTOM_VOLUME = "عدد حجم را به گیگابایت بفرستید — مثلاً ۳۵"
# (The old RENEW_BUTTON/BUY_BUTTON went with T3/T6: the card carries
# CARD_RENEW_BUTTON and the menu carries MENU_BUY — one name each.)
CANCEL = "❌ انصراف"

# ── T4 buy flow — one screen, one action ──────────────────────────────────

# Inline button on the volume screen: the OPTIONAL pre-pick of the Marzban
# username. Default stays the auto shop{id} name — the ask is off the
# mandatory path entirely (design decision 3, docs/ZCODE_SHOPBOT_UX_DESIGN.md).
PREFERRED_NAME_BUTTON = "✏️ نام کاربری دلخواه"
ASK_PREFERRED_NAME = (
    "نام کاربری سرویس را انتخاب کنید (اختیاری):\n"
    "حروف انگلیسی، عدد و - یا _ — بین ۳ تا ۳۰ کاراکتر، مثلاً fateme.\n"
    "همین نام در اپ و اشتراکتان دیده می‌شود. «بدون نام» = خودکار (مثل shop10)."
)
# An old inline button whose flow has already moved on (or died with a
# restart): answered, never silence.
STALE_BUTTON = "این دکمه قدیمی شده — از منوی پایین ادامه دهید."

# Offered once, after a first real purchase (never on /start — see
# handlers/shop.py) — never required for anything.
SHARE_PHONE_BUTTON = "📞 اشتراک‌گذاری شماره"
PHONE_LATER = "بعداً"

SHARE_PHONE_PROMPT = (
    "اگر مایل بودید، شماره‌تان را هم بفرستید — "
    "اگر این آیدی تلگرام را از دست دادید، با همین شماره موجودی‌تان را برمی‌گردانیم.\n"
    "کاملاً اختیاری است."
)
PHONE_SAVED = "ثبت شد، ممنون 🙏"
PHONE_LATER_ACK = "باشد، مشکلی نیست."


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
        "سفارش اول ثبت می‌شود، بعد پرداخت:\n"
        f"۱. از «{MENU_BUY}» حجم را انتخاب کنید.\n"
        "۲. مبلغ دقیق و شماره کارت را می‌بینید؛ عکس رسید را همینجا بفرستید یا کد پیگیری تراکنش را تایپ کنید.\n"
        f"۳. رسید که بررسی شد (معمولاً تا {fa(eta_minutes)} دقیقه)، لینک و QR سرویس همینجا می‌آید.\n\n"
        f"هر گیگابایت {money(price_per_gb)}؛ سرویس‌ها {fa(days)} روزه‌اند. با موجودیِ کافی، خرید همان لحظه انجام می‌شود."
        + support(handle)
    )


# ── buying ────────────────────────────────────────────────────────────────

# Shown once, above buy_prompt, only for MENU_NEW_SERVICE - says plainly
# what this button does differently from the normal "🛒 خرید سرویس" (which
# just adds to the existing service), so nobody taps it by mistake expecting
# a top-up and nobody taps MENU_BUY expecting a second, separate service.
NEW_SERVICE_INTRO = (
    "این گزینه سرویسِ جداگانه می‌سازد — سرویس فعلی‌تان دست‌نخورده می‌ماند و لینکش کار می‌کند."
)


def buy_prompt(price_per_gb: int, min_gb: float, max_gb: float, days: int) -> str:
    # Leads with the choice itself (price-first reads as a bill before any
    # sense of what's on offer); the rate is still stated plainly right
    # after, nothing hidden - this is reframing, not a discount.
    return (
        f"از {gb(min_gb)} تا {gb(max_gb)}، همه {fa(days)} روزه.\n"
        f"هر گیگابایت {money(price_per_gb)}."
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
        f"موجودی شما: {money(balance)}\n\n"
        "از موجودی‌تان کم می‌شود و سرویس همین الان ساخته می‌شود."
    )


BUY_WORKING = "دارم سرویس‌تان را می‌سازم… چند لحظه."


# ── "⚡ تمدید خودکار (میانگین مصرفم)" — the bounded-debt auto-renew button ──

def auto_renew_ok(volume_gb: float) -> str:
    """Balance stayed non-negative — reads like any other successful
    renewal, no debt framing at all."""
    return f"🎉 سرویستان با {gb(volume_gb)} (میانگین مصرفتان) تمدید شد — همین الان فعال است."


def auto_renew_in_debt(volume_gb: float, owed: int, grace_days: int) -> str:
    """Balance went negative. States the real number and the real deadline —
    never a vague "حتماً شارژ کنید"."""
    return (
        f"🎉 سرویستان با {gb(volume_gb)} (میانگین مصرفتان) تمدید شد — همین الان فعال است.\n\n"
        f"موجودی‌تان {money(owed)} منفی شد. تا {fa(grace_days)} روز دیگر برای شارژ وقت دارید؛ "
        "وگرنه سرویس موقتاً متوقف می‌شود (با شارژ کردن، خودش دوباره فعال می‌شود)."
    )


AUTO_RENEW_ALREADY_IN_DEBT = (
    "یک تمدید خودکار قبلی هنوز تسویه نشده.\n"
    "برای ادامه، موجودی‌تان را شارژ کنید یا از کارت سرویس، «تمدید» را بزنید."
)

AUTO_RENEW_NO_HISTORY = (
    "برای این سرویس هنوز سابقه‌ی خریدی نیست که میانگینش را بگیرم.\n"
    "از کارت سرویس، «تمدید» را بزنید."
)


# ── pay-as-you-go ("📊 سرویس مصرفی") ───────────────────────────────────────

def payg_intro(rate_per_gb: int) -> str:
    return (
        "حجم ثابتی نمی‌خرید — هر روز فقط به‌اندازهٔ مصرفتان، "
        f"{money(rate_per_gb)} برای هر گیگ، از موجودی‌تان کم می‌شود.\n"
        "موجودی که تمام شود سرویس موقتاً متوقف می‌شود و با شارژ، خودش دوباره وصل می‌شود.\n"
        "همین حالا ساخته می‌شود؛ تا مصرفی نداشته باشید چیزی کم نمی‌شود."
    )


PAYG_UNAVAILABLE = "سرویس مصرفی الان فعال نیست."
PAYG_STARTED_CONFIRM = "✅ سرویس مصرفی‌تان فعال شد — مصرف روزانه از موجودی‌تان کم می‌شود."

PAYG_ALREADY_HAS_SERVICE = (
    "شما همین الان یک سرویس فعال دارید.\n"
    "سرویس مصرفی برای کسی است که هنوز هیچ سرویسی نگرفته."
)


def not_a_number(example: str) -> str:
    return f"لطفاً فقط عدد بفرستید، مثلاً {example}."


def out_of_range(min_gb: float, max_gb: float) -> str:
    return f"حجم باید بین {gb(min_gb)} و {gb(max_gb)} باشد."


# ── the trial ─────────────────────────────────────────────────────────────

TRIAL_ALREADY_TAKEN = (
    "تست رایگانتان را قبلاً گرفته‌اید 🙂\n"
    f"برای ادامه از «{MENU_BUY}» یک سرویس بگیرید."
)
TRIAL_UNAVAILABLE = "تست رایگان فعلاً فعال نیست."


# ── paying ────────────────────────────────────────────────────────────────

NEED_PHOTO = (
    "این را به‌عنوان رسید متوجه نشدم.\n"
    "عکس رسید را بفرستید، یا کد پیگیری تراکنش را تایپ کنید."
)

RECEIPT_ALREADY_WAITING = (
    "رسید قبلی‌تان برای همین سفارش در صف بررسی است؛ "
    "لازم نیست دوباره بفرستید. "
    "تا تأیید، همینجا خبرتان می‌کنم."
)

RECEIPT_AS_FILE = (
    "این فایل را نمی‌توانم باز کنم. "
    "رسید را به صورت عکس بفرستید، یا کد پیگیری تراکنش را تایپ کنید؛ "
    "سفارشتان محفوظ است."
)

RECEIPT_WITHOUT_CONTEXT = (
    "این عکس مربوط به چه پرداختی است؟\n"
    f"اول از «{MENU_BUY}» سرویس را انتخاب کنید یا از «{MENU_ACCOUNT}» شارژ کنید، بعد رسید را بفرستید."
)


def topup_ask_amount(min_topup: int, max_topup: int) -> str:
    return (
        "چه مبلغی می‌خواهید شارژ کنید؟ عدد را به تومان بفرستید (مثلاً ۲۰۰,۰۰۰).\n"
        f"از {money(min_topup)} تا {money(max_topup)}."
    )


def topup_out_of_range(min_topup: int, max_topup: int) -> str:
    return f"مبلغ شارژ باید بین {money(min_topup)} و {money(max_topup)} باشد. یک مبلغ دیگر بفرستید."


def plain_digits(card_number: str) -> str:
    """Digits only. Iranian banking apps reject a pasted card number that
    contains dashes or spaces, so what the customer taps to copy must be bare
    even if the operator typed it grouped."""
    return "".join(ch for ch in card_number if ch.isdigit()) or card_number


def md_escape(value: str) -> str:
    """Neutralises the four characters legacy Markdown treats as formatting.

    These messages are sent with parse_mode=MARKDOWN for the tap-to-copy
    spans, and the card holder's name comes from the operator's settings. A
    single underscore in it made Telegram reject the whole message, so the
    customer got the generic error instead of the card to pay into.
    """
    for ch in ("_", "*", "`", "["):
        value = value.replace(ch, "\\" + ch)
    return value


def topup_instructions(amount: int, card_number: str, card_holder: Optional[str], eta_minutes: int) -> str:
    """The payment request.

    The amount is shown in Persian digits for reading, and AGAIN as a bare
    Latin number in a tap-to-copy span for the bank app — which settles the
    "Persian looks right / Latin pastes right" tension by giving each its own
    line instead of picking one.

    The last line exists because of how paying actually goes in Iran: banking
    apps refuse to open over a VPN, so the customer turns the VPN off, and
    Telegram drops with it. Without being told their order survives that, a
    newcomer reasonably assumes it doesn't.
    """
    holder = f"\nبه نام: {md_escape(card_holder)}" if card_holder else ""
    # Reassurance first (the order is recorded, nothing is lost), THEN the
    # logistics, THEN the wait time — framed around what happens
    # automatically once it's reviewed, not as a standalone delay. The
    # bank-app/VPN tip stays (a real failure mode, cutting it creates real
    # support tickets) but moves out of the closing line, so the message
    # doesn't end on a potential problem.
    return (
        f"سفارشتان ثبت شد و محفوظ می‌ماند.\n\n"
        f"برای تکمیل، مبلغ {money(amount)} را به این کارت واریز کنید:\n\n"
        f"`{plain_digits(card_number)}`{holder}\n"
        f"مبلغ برای کپی: `{amount}`\n\n"
        "اگر برنامه‌ی بانک با VPN باز نمی‌شود، برای پرداخت خاموشش کنید — سفارشتان همچنان محفوظ است.\n\n"
        "بعد عکس رسید را همینجا بفرستید، یا کد پیگیری تراکنش را تایپ کنید؛ "
        f"معمولاً تا {fa(eta_minutes)} دقیقه بررسی و سرویستان خودکار آماده می‌شود."
    )


def topup_instructions_with_minimum(amount: int, owed: int, card_number: str,
                                    card_holder: Optional[str], eta_minutes: int) -> str:
    """The payment request when the order's debt is UNDER the shop's own
    minimum top-up: the customer is asked for the minimum instead, never the
    bare shortfall. Two reasons, one per side:

    The shop's side: card-to-card under a few thousand Toman is exactly the
    transfer Iranian banking apps make painful (some refuse it outright) —
    asking for it manufactures a failed payment and a support conversation.

    The customer's side: the surplus is not a tip, it is CREDIT — the
    approval pays the order in full and puts the remainder back in the
    wallet, so the promise below is literal. The backend accepts an
    order-bound receipt at any amount, so a customer who transfers only the
    debt anyway is still fine; the minimum is the polite ask, not a gate.
    """
    holder = f"\nبه نام: {md_escape(card_holder)}" if card_holder else ""
    return (
        f"سفارشتان ثبت شد و محفوظ می‌ماند.\n\n"
        f"بدهی این سفارش {money(owed)} است؛ لطفاً حداقل {money(amount)} به این کارت واریز کنید:\n\n"
        f"`{plain_digits(card_number)}`{holder}\n"
        f"مبلغ برای کپی: `{amount}`\n\n"
        "مازادش به موجودی‌تان برمی‌گردد و برای خرید بعدی‌تان می‌ماند.\n\n"
        "اگر برنامه‌ی بانک با VPN باز نمی‌شود، برای پرداخت خاموشش کنید — سفارشتان همچنان محفوظ است.\n\n"
        "بعد عکس رسید را همینجا بفرستید، یا کد پیگیری تراکنش را تایپ کنید؛ "
        f"معمولاً تا {fa(eta_minutes)} دقیقه بررسی و سرویستان خودکار آماده می‌شود."
    )


NO_CARD = "هنوز شماره کارت ثبت نکرده‌ام. یک لحظه پیام بدهید تا درستش کنم."


def receipt_failed(handle: Optional[str]) -> str:
    """The receipt upload itself failed. Says plainly that nothing was
    recorded — and that nothing was lost by us — so resending is obviously
    safe rather than feeling like paying twice."""
    return (
        "رسیدتان ثبت نشد — لطفاً همان رسید (عکس یا کد پیگیری) را دوباره بفرستید.\n"
        "از طرف ما پولی جابه‌جا نشده که از دست برود." + support(handle)
    )


CODE_NOT_FOUND = "پرداختی با این کد پیدا نکردم. کد را همان‌طور که در پیام رسید آمده بفرستید."


def payment_status(status: str, reference: str, order_status: Optional[str],
                   data_limit_gb: Optional[float], reject_reason: Optional[str],
                   handle: Optional[str]) -> str:
    """The answer to "what happened to my payment?", given by the bot itself
    the moment the customer types their code — at any hour, without waiting
    for a person."""
    if status == "pending":
        return (
            f"⏳ پرداخت {reference} هنوز در صف بررسی است.\n"
            "به محض بررسی همینجا خبر می‌دهم." + support(handle)
        )
    if status == "rejected":
        why = f"\nعلت: {reject_reason}" if reject_reason else ""
        return f"❌ پرداخت {reference} تأیید نشد.{why}" + support(handle)
    if order_status == "delivered":
        what = f"سرویس {gb(data_limit_gb)}" if data_limit_gb else "سرویس‌تان"
        return f"✅ پرداخت {reference} تأیید شد و {what} تحویل شد — در «{MENU_ACCOUNTS}» است."
    if order_status == "awaiting_payment":
        return (
            f"✅ پرداخت {reference} تأیید شد ولی برای سرویسِ انتخابی کافی نبود.\n"
            f"برای کامل‌کردن، از «{MENU_BUY}» ادامه دهید؛ سفارشتان سر جایش است." + support(handle)
        )
    return f"✅ پرداخت {reference} تأیید شد و به موجودی‌تان اضافه شد."


# ── wallet & accounts ─────────────────────────────────────────────────────


def debt_line(debt: float) -> str:
    """The reminder shown when a wallet top-up starts while a debt exists —
    the owner's «موقع شارژ بهش گفته بشه»."""
    return f"ⓘ یادآوری: ماندهٔ پرداخت‌نشده‌ی شما {money(int(debt))} است. اگر قصد پرداخت آن را دارید، از «{MENU_ACCOUNT}» ادامه دهید."


# ── the merged «حساب من» screen (T2) — ONE number per view ────────────────

def account_wallet_view(balance: int, recent_lines: list[str]) -> str:
    """The positive/zero side of «حساب من»: the balance, one honest hint, and
    at most three recent transactions. Never shows a debt figure — the debt
    has its own view (account_debt_view) and the two never render side by
    side."""
    if balance < 0:
        # Only the payg meter and the bounded auto-renew can take the wallet
        # negative. "منفی X" in words — a bare minus next to Persian digits
        # detaches in RTL rendering (see confirm_from_wallet's comment).
        lines = [f"موجودی شما: منفی {money(-int(balance))}",
                 "با شارژ کردن، اول همین عدد جبران می‌شود."]
    else:
        lines = [f"موجودی شما: {money(balance)}"]
        if balance == 0:
            lines.append("برای خرید لازم نیست از قبل شارژ کنید — مستقیم از «" + MENU_BUY + "» شروع کنید.")
    if recent_lines:
        lines += ["", "آخرین تراکنش‌ها:", *recent_lines]
    return "\n".join(lines)


def account_debt_view(debt: float, exact: bool = True) -> str:
    """The owing side of «حساب من»: ONE number, then the buttons carry the
    actions. The per-account breakdown appears only behind «جزئیات» — never
    by default."""
    lines = [f"ماندهٔ پرداخت‌نشده: {money(int(debt))}"]
    if not exact:
        lines += ["", DEBT_NOT_EXACT]
    return "\n".join(lines)


def debt_receipt_amount_ask(debt: int, max_topup: int) -> str:
    """The amount ask for a debt payment by card receipt. The whole debt is
    one button away; typing a smaller number still works (anti-fat-finger
    floor 1,000 — see handlers)."""
    return (
        f"ماندهٔ پرداخت‌نشده: {money(debt)}\n\n"
        f"«{DEBT_AMOUNT_AS_IS}» را بزنید تا رسیدِ پرداختِ کامل را ثبت کنیم؛ "
        "یا اگر می‌خواهید کمتر بپردازید، همان مبلغ را بفرستید.\n"
        f"(حداقل ۱٬۰۰۰ — حداکثر {money(max_topup)})"
    )


def debt_status(debt: float, breakdown=None, exact: bool = True) -> str:
    """The «جزئیات» view behind the debt screen (T2/T5): what they owe, and
    — only here, by request — whose account each piece belongs to. No
    spendable-arithmetic line: that was the system doing bookkeeping at the
    customer (T0)."""
    lines = [
        f"ماندهٔ پرداخت‌نشده: {money(int(debt))}",
        *[debt_source_line(row) for row in (breakdown or [])],
        "",
        "تا وقتی این مانده پرداخت نشود، خرید سرویس جدید ممکن نیست.",
    ]
    if not exact:
        lines += ["", DEBT_NOT_EXACT]
    return "\n".join(lines)


DEBT_PAY_BY_RECEIPT = "ارسال رسید کارت‌به‌کارت"
DEBT_NONE = "ماندهٔ پرداخت‌نشده‌ای ندارید ✓"


def debt_receipt_instructions(amount: int, card: str, holder, eta: int) -> str:
    return (
        f"مبلغ {money(amount)} را به کارت زیر واریز کنید و رسید آن را "
        "همین‌جا بفرستید. این پرداخت روی ماندهٔ پرداخت‌نشده‌تان ثبت می‌شود "
        "(شارژ موجودی نمی‌شود — ماندهٔ پرداخت‌نشده کم می‌شود).\n\n"
        f"شماره کارت: `{card}`\n"
        + (f"به نام {holder}\n" if holder else "")
        + f"\nمعمولاً تا {fa(eta)} دقیقه بررسی می‌شود."
    )


def debt_receipt_done(reference, eta: int) -> str:
    lines = ["✅ رسید پرداخت بدهی شما دریافت شد.",
             f"معمولاً تا {fa(eta)} دقیقه بررسی و روی صورت‌حسابتان ثبت می‌شود.",
             "لازم نیست ربات را باز نگه دارید — خبرش همین‌جا می‌آید."]
    if reference:
        lines.append(f"کد پیگیری این پرداخت: {reference}")
    return "\n".join(lines)


def debt_paid_from_wallet(paid: int, wallet: int, debt_left: float) -> str:
    left = (
        f"بدهی باقی‌مانده: {money(int(debt_left))}"
        if debt_left > 0.5 else "بدهی شما کامل پرداخت شد ✓"
    )
    return (
        f"✅ {money(paid)} پرداخت شد و روی ماندهٔ شما ثبت شد.\n"
        f"موجودی شما: {money(wallet)}\n"
        f"{left}"
    )


def debt_source_line(row: dict) -> str:
    """One «where this debt comes from» row; a negative amount is a payment."""
    amount = int(row["debt"])
    shown = money(amount) if amount >= 0 else f"منفی {money(-amount)}"
    mark = "" if row.get("exact", True) else " (تقریبی)"
    return f"   • {row['name']}: {shown}{mark}"


DEBT_NOT_EXACT = (
    "این عدد تقریبی است — اگر با حساب خودتان نمی‌خواند، پیام بدهید تا بررسی کنم."
)


WALLET_EMPTY = (
    "موجودی‌تان صفر است.\n"
    f"برای خرید لازم نیست از قبل شارژ کنید — کافی است «{MENU_BUY}» را بزنید."
)

NO_ACCOUNTS = f"هنوز سرویسی نگرفته‌اید. از «{MENU_BUY}» شروع کنید."

# ── the service card (T3) — the card carries its own actions ──────────────

# One tap from the card starts the renewal of the same size (the same
# renew: callback the backend's expiry warnings carry).
CARD_RENEW_BUTTON = "🔄 تمدید"
CARD_MORE_BUTTON = "⋯ بیشتر"
SERVICE_MORE_HINT = "تنظیمات همین سرویس:"
AUTO_RENEW_BUTTON = "⚡ تمدید خودکار (میانگین مصرفم)"
# Under an ended card (volume gone or days up): what happened, and what one
# tap does — stated as information, never an order.
RENEW_HINT_ENDED = "سرویس قطع است. دکمهٔ «تمدید» همان حجم را دوباره فعال می‌کند."


def usage_bar(used_gb: float, total_gb: float, width: int = 10) -> str:
    """A ten-segment filled/empty bar. The customer's eye reads shape before
    it reads numbers — "how full is this thing" must be answerable at a
    glance, not by subtracting two decimals."""
    if total_gb <= 0:
        filled = width
    else:
        filled = round(min(1.0, used_gb / total_gb) * width)
    return "▰" * filled + "▱" * (width - filled)


# Shown under a service whose account row is "disabled" — the status both
# auto-suspensions (wallet debt, payg low balance) and an operator's manual
# stop produce, indistinguishable from here because the backend sends only
# the status string. Honest and blame-free: states that the service IS
# stopped — without it the row still renders "active, X GB left" and reads
# as a broken promise the day the customer notices nothing connects.
STATUS_SUSPENDED_BADGE = "⚪️ موقتاً متوقف — با شارژ، خودش دوباره وصل می‌شود"

# ── naming a service (the customer's own nickname) ────────────────────────
# ONE string: a stray comma here once made this a tuple, and python-telegram-bot
# sent its JSON repr («["نام …"]») to a customer (2026-10-26).
ASK_SERVICE_NAME = (
    "نام سرویس را بفرستید — هر اسمی که برایتان مفهوم است، مثلاً «گوشی علی».\n"
    "برای حذفِ نام فعلی، «حذف» را بفرستید."
)
SERVICE_NAME_SAVED = "✅ ثبت شد — از این به بعد سرویس‌تان با همین نام نشان داده می‌شود."
SERVICE_NAME_CLEARED = "نام حذف شد — سرویس‌تان دوباره با حجمش نام‌گذاری می‌شود."
SERVICE_NAME_TOO_LONG = "نام طولانی است — تا ۴۰ کاراکتر."

SKIP_NAME = "بدون نام"
PURCHASE_CANCELLED = f"خرید لغو شد — هر وقت خواستید، از «{MENU_BUY}» شروع کنید."


def account_line(volume_gb: Optional[float], used_gb: float, days_left: Optional[int],
                 status: Optional[str] = None, name: Optional[str] = None) -> str:
    """One subscription, named by what it IS rather than by its panel username.

    The previous version printed the raw Marzban username — a Latin identifier
    the customer cannot use, cannot read, and which leaked the panel
    software's name into a consumer screen.

    Three renderings by state, because one shape cannot fit all:
      capped + volume left  → bar, remaining gigabytes FIRST (that is the
                              number they act on), then the percentage
      capped + volume gone  → plainly finished, no bar to squint at
      unlimited             → no bar and no percentage at ALL — dividing by
                              a fabricated cap is how a name-unlimited
                              service got shown as "94% used" (live
                              2026-10-01). Usage is a plain "so far" figure.

    `status` is optional (the backend's accounts list always carries it; the
    default None keeps every rendering byte-identical for callers that don't
    pass it). Only a suspended row changes the output — an active, limited
    or expired service already says its state through the bar / «تمام شد»
    lines above, and those stay exactly as they were.
    """
    if days_left is None:
        life = "بدون انقضا"
    elif days_left <= 0:
        life = "تمام شده"
    else:
        life = f"{fa(days_left)} روز مانده"
    # The customer's own nickname (set via «✏️ نام» on the service) becomes
    # its title — the thing they actually call it in their head. Without one,
    # the size-based default title keeps every rendering byte-identical.
    title = f"🔑 «{name}» — سرویس" if name else "🔑 سرویس"
    if volume_gb is None:
        text = f"{title} نامحدود\nمصرف تا امروز: {fa(round(used_gb, 2))} گیگ\n{life}"
    else:
        remaining = max(0.0, volume_gb - used_gb)
        if remaining <= 0:
            text = f"{title} {gb(volume_gb)}\n🔴 حجم سرویس تمام شد\n{life}"
        else:
            pct = int(used_gb * 100 / volume_gb) if volume_gb else 0
            bar = f"{usage_bar(used_gb, volume_gb)} {fa(round(remaining, 2))} گیگ باقی مانده ({fa(pct)}٪ مصرف شده)"
            text = f"{title} {gb(volume_gb)}\n{bar}\n{life}"
    if status == "disabled":
        text += "\n" + STATUS_SUSPENDED_BADGE
    return text


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


# The instant wallet purchase whose account build failed (backend 502 on
# /orders/{id}/pay). The backend refunds the wallet BEFORE answering, so the
# honest answer to the customer's real question — "did I just lose my
# money?" — is no, and this text says so plainly instead of the generic
# "کاری انجام نشد", which after a wallet debit reads as "my money vanished".
PURCHASE_FAILED_REFUNDED = (
    "ساخت سرویس نیمه‌کاره ماند و انجام نشد.\n"
    "پرداختتان به موجودی‌تان برگشت — سالم است.\n"
    f"چند لحظه بعد دوباره از «{MENU_BUY}» شروع کنید."
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

# For a cancel tap that arrives when nothing is in progress — most often one
# that was queued behind a purchase already going through. Saying "cancelled"
# there told a customer whose service was on its way that it wasn't.
NOTHING_TO_CANCEL = "کاری در جریان نبود که لغو شود. اگر خریدی را تأیید کرده بودید، ادامه‌اش همینجا می‌آید."


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


# ── shop-link invite (t.me/<bot>?start=shoplnk_...) ───────────────────────
#
# The operator's own customers — people whose service was created by hand in
# the panel, before this bot existed — enter through a one-time deep link the
# operator sends them, not through buying. The texts below are the outcomes
# of tapping that link, worded for someone who already IS a customer: they
# are being welcomed in, not sold to.

# 404 from the backend covers three indistinguishable-to-the-user cases
# (unknown token, already-used token, expired) — one text for all of them,
# always ending in the same instruction: ask for a fresh link. Same shape as
# delegate_bot's INVITE_INVALID_OR_EXPIRED.
LINK_INVALID_OR_EXPIRED = (
    "این لینک دعوت معتبر نیست یا منقضی شده است.\n"
    "از مدیریت بخواهید لینک تازه‌ای برایتان بفرستد."
)

# 409: the binding would contradict one that already exists — either this
# Telegram account is already connected to another customer's services, or
# the customer this link belongs to is already connected to a different
# Telegram account (from the tapper's side the two look the same). One text
# for both, ending in the same place: ask the operator to sort it out.
LINK_ALREADY_CONNECTED = (
    "این لینک الان قابل استفاده نیست — این اکانت تلگرام یا مشتریِ پشت این لینک، "
    "قبلاً به یک حساب دیگر وصل شده است.\n"
    "لطفاً به مدیریت بگویید تا بررسی کند."
)


def link_welcome(customer_name: str, accounts_linked: int) -> str:
    """The moment the link lands: whose services got connected, how many of
    them, and the two things they can now do — see status, and renew in
    place (the renewal sentence is the whole point of the feature: the link
    in their VPN app keeps working, nothing needs re-importing)."""
    return (
        f"سلام {customer_name} عزیز 👋\n"
        f"سرویس‌هایتان وصل شد — {fa(accounts_linked)} سرویس فعلی به این ربات اضافه شد.\n\n"
        f"از «{MENU_ACCOUNTS}» وضعیت سرویس‌هایتان را ببینید.\n"
        f"برای تمدید هم کافی است از «{MENU_BUY}» خرید کنید — حجم تازه به همان سرویس فعلی‌تان "
        "اضافه می‌شود و لینکی که در برنامه دارید بدون تغییر کار می‌کند."
    )


# The unlimited-service refusal (backend 409 "SERVICE_IS_UNLIMITED"). Leads
# with the fact, then the two things an anxious customer needs: no money
# moved, and there IS a path for the rare case they actually want a second
# service.
SERVICE_UNLIMITED = (
    "سرویس فعلی شما نامحدود است و حجم اضافه نمی‌خواهد — برای همین خریدی ثبت نشد "
    "و پولی از موجودی‌تان کم نشد.\n"
    "اگر برای دستگاه دیگری سرویس جداگانه می‌خواهید، به پشتیبانی پیام بدهید تا برایتان بسازیم."
)

# The payg-refusal twin of SERVICE_UNLIMITED (backend 409 "PAYG_TOPUP_INSTEAD"):
# the customer's current service is metered — it needs wallet credit, not a
# fixed-volume package, and the backend refuses the order before any money
# moves. Same shape as its twin: the fact, the way forward, and the
# no-money-moved reassurance.
PAYG_TOPUP_INSTEAD = (
    "سرویس فعلی شما مصرفی است — به‌جای بسته، موجودی‌تان را شارژ کنید؛ "
    "مصرف روزانه از آن کم می‌شود.\n"
    "این خرید ثبت نشد و پولی از موجودی‌تان کم نشد."
)
