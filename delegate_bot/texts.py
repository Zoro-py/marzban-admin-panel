"""Every string the delegate bot says in reply to a tap.

Plain text throughout, no parse_mode — see bot.py's on_error for why: the
only ever-embedded values here are auto-generated usernames (e.g. "d17")
and numbers, never customer-typed free text, so there is nothing that needs
Markdown escaping and no reason to add the risk of a malformed message.

NUMBERS: Persian digits for anything the delegate only reads (fa()); plain
Latin digits for a username, since that is typed into an app, not retyped
by hand — same reasoning as shopbot/texts.py's own split.
"""

from __future__ import annotations

_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa(value) -> str:
    if isinstance(value, float) and value != int(value):
        text = f"{value:,.2f}".rstrip("0").rstrip(".").replace(".", "٫")
    else:
        text = f"{int(value):,}"
    return text.translate(_PERSIAN_DIGITS)


def gb(value: float) -> str:
    return f"{fa(value)} گیگ"


NOT_A_DELEGATE = (
    "شما به این بات دسترسی ندارید.\n"
    "اگر فکر می‌کنید این اشتباه است، با تیم فروش تماس بگیرید."
)

GENERIC_ERROR = "یک مشکل پیش آمد — چند لحظه دیگر دوباره امتحان کنید."

MENU_NEW = "➕ اکانت جدید"
MENU_LIST = "📋 اکانت‌های من"


def welcome(scope_name: str) -> str:
    return (
        f"سلام! این بات مخصوص «{scope_name}» است.\n"
        "از اینجا می‌توانید خودتان اکانت بسازید، تمدید کنید یا حذف کنید — "
        "بدون نیاز به هماهنگی با تیم فروش برای هرکدام.\n\n"
        "«📋 اکانت‌های من» را بزنید تا فهرست اکانت‌های فعلی را ببینید، "
        "یا «➕ اکانت جدید» برای ساخت یکی تازه."
    )


def pick_volume_for_new() -> str:
    return "چند گیگ؟"


def account_created(username: str, data_limit_gb: float, duration_days: int) -> str:
    return (
        f"✅ ساخته شد: {username}\n"
        f"حجم: {gb(data_limit_gb)} — مدت: {fa(duration_days)} روز\n\n"
        "لینک اشتراک تا حدود یک دقیقه دیگر آماده می‌شود — «📋 اکانت‌های من» را بزنید."
    )


def no_accounts_yet() -> str:
    return "هنوز هیچ اکانتی نساخته‌اید — «➕ اکانت جدید» را بزنید."


def account_line(marzban_username: str, data_limit: int | None, expire: int | None,
                 used_traffic: int, status: str | None, subscription_url: str | None) -> str:
    import time

    limit_line = gb(data_limit / (1024 ** 3)) if data_limit else "نامحدود"
    used_line = gb(used_traffic / (1024 ** 3))
    if expire:
        remaining_days = max(0, round((expire - time.time()) / 86400))
        expire_line = f"{fa(remaining_days)} روز مانده"
    else:
        expire_line = "بدون انقضا"
    status_icon = {"active": "🟢", "limited": "🔴", "expired": "🔴", "disabled": "⚪"}.get(status or "", "⚪")
    lines = [f"{status_icon} {marzban_username} — {used_line} از {limit_line} — {expire_line}"]
    if subscription_url:
        lines.append(subscription_url)
    return "\n".join(lines)


def pick_volume_for_renew() -> str:
    return "چند گیگ اضافه شود؟"


def renewed(username: str, extend_gb: float, extend_days: int) -> str:
    return f"✅ {username} تمدید شد: +{gb(extend_gb)} / +{fa(extend_days)} روز"


def confirm_delete(username: str) -> str:
    return f"⚠️ اکانت «{username}» برای همیشه حذف می‌شود و برنمی‌گردد. مطمئنید؟"


def deleted(username: str) -> str:
    return f"🗑 اکانت «{username}» حذف شد."


def delete_cancelled() -> str:
    return "انصراف داده شد — چیزی حذف نشد."
