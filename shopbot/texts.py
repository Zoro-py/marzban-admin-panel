"""Every string a shop customer sees, in one place.

Persian, unlike the rest of this codebase, because these are read by the
customer and not by the operator. Anything the OPERATOR sees — logs,
exceptions, the dashboard — stays English.

Kept in one module rather than inline in the handlers so the wording can be
reviewed as a whole. A shop's voice is inconsistent the moment its sentences
live in eight different files.

Numbers are formatted with Latin digits and thousands separators. Persian
digits look better in isolation but are routinely mangled when a customer
copies an amount into a banking app, and an amount that arrives wrong is a
support conversation.
"""

MENU_BUY = "🛒 خرید اشتراک"
MENU_WALLET = "💰 کیف پول"
MENU_ACCOUNTS = "📱 اشتراک‌های من"
MENU_TOPUP = "➕ افزایش موجودی"
MENU_HELP = "❓ راهنما"

WELCOME = (
    "سلام 👋\n"
    "به ربات فروش اشتراک خوش آمدید.\n\n"
    "از منوی پایین یکی را انتخاب کنید."
)

HELP = (
    "راهنمای استفاده:\n\n"
    "۱. اول از «افزایش موجودی» کیف پولتان را شارژ کنید — مبلغ را کارت‌به‌کارت "
    "می‌کنید و عکس رسید را می‌فرستید.\n"
    "۲. بعد از تأیید پرداخت توسط پشتیبانی، موجودی کیف پولتان زیاد می‌شود.\n"
    "۳. از «خرید اشتراک» حجم دلخواهتان را انتخاب کنید؛ هزینه از کیف پول کم می‌شود "
    "و بلافاصله لینک و QR اشتراک برایتان ارسال می‌شود.\n\n"
    "همه‌ی اشتراک‌ها یک‌ماهه هستند و فقط حجمشان فرق می‌کند."
)

SHOP_CLOSED = "فروش موقتاً بسته است. لطفاً بعداً دوباره امتحان کنید."
BLOCKED = "امکان خرید با این حساب وجود ندارد. با پشتیبانی تماس بگیرید."

# Deliberately not the raw exception text. A customer reading a Python error
# learns nothing useful and it tells an attacker about the internals.
GENERIC_ERROR = "مشکلی پیش آمد. لطفاً چند لحظه بعد دوباره تلاش کنید."

CANCELLED = "لغو شد."


def money(amount: int) -> str:
    return f"{amount:,} تومان"


def gb(value: float) -> str:
    return f"{value:g} گیگابایت"


def wallet_summary(balance: int) -> str:
    return f"💰 موجودی کیف پول شما: {money(balance)}"


def buy_prompt(price_per_gb: int, min_gb: float, max_gb: float, duration_days: int) -> str:
    return (
        f"قیمت هر گیگابایت: {money(price_per_gb)}\n"
        f"مدت همه‌ی اشتراک‌ها: {duration_days} روز\n"
        f"حداقل {gb(min_gb)} و حداکثر {gb(max_gb)}\n\n"
        "یکی از حجم‌های آماده را انتخاب کنید، یا عدد حجم دلخواهتان را بفرستید (مثلاً ۳۵)."
    )


def confirm_purchase(volume: float, price: int, duration_days: int, balance: int) -> str:
    return (
        f"تأیید خرید:\n\n"
        f"حجم: {gb(volume)}\n"
        f"مدت: {duration_days} روز\n"
        f"قیمت: {money(price)}\n\n"
        f"موجودی فعلی: {money(balance)}\n"
        f"موجودی پس از خرید: {money(balance - price)}"
    )


def topup_instructions(amount: int, card_number: str, card_holder: str | None) -> str:
    holder = f"\nبه نام: {card_holder}" if card_holder else ""
    return (
        f"مبلغ {money(amount)} را به این کارت واریز کنید:\n\n"
        f"`{card_number}`{holder}\n\n"
        "بعد از واریز، **عکس رسید** را همینجا بفرستید.\n"
        "پرداخت شما پس از بررسی توسط پشتیبانی تأیید می‌شود و موجودی کیف پولتان زیاد می‌شود."
    )


TOPUP_ASK_AMOUNT = "چه مبلغی می‌خواهید شارژ کنید؟ عدد را به تومان بفرستید (مثلاً ۲۰۰۰۰۰)."
TOPUP_ASK_RECEIPT = "حالا عکس رسید پرداخت را بفرستید."
TOPUP_NOT_A_NUMBER = "لطفاً فقط عدد بفرستید، مثلاً ۲۰۰۰۰۰."
TOPUP_NEED_PHOTO = (
    "لطفاً رسید را به صورت **عکس** بفرستید، نه متن.\n"
    "اگر رسیدتان فایل PDF است، از آن اسکرین‌شات بگیرید."
)
TOPUP_SUBMITTED = (
    "✅ رسید شما ثبت شد و برای بررسی ارسال شد.\n"
    "به محض تأیید، همینجا به شما اطلاع می‌دهیم."
)
TOPUP_NO_CARD = "شماره کارت هنوز تنظیم نشده است. لطفاً با پشتیبانی تماس بگیرید."

NO_ACCOUNTS = "هنوز اشتراکی نخریده‌اید. از «خرید اشتراک» شروع کنید."

PURCHASE_SENDING = "در حال ساخت اشتراک… چند لحظه صبر کنید."
