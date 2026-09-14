"""Persian the BACKEND sends to a shop customer.

There are two customer-facing text modules and the split is by SENDER, not by
topic:

  * `shopbot/texts.py`   — what the bot says in reply to a tap. It has the
                           conversation in front of it.
  * this file            — what the backend pushes when there is no open
                           request to reply to: an operator approving a
                           payment, a subscription being delivered, a plan
                           about to expire.

Delivery lives HERE rather than in the bot even though the bot could send it,
because delivery happens on two different triggers — an instant purchase from
a funded wallet, and an operator approving a card payment hours later. Only
the backend sees both. Putting the wording in the bot would mean two copies of
the most important message in the product, and they would drift.

Everything here is written to be read on a phone by someone who may have just
sent money to a stranger. Short lines, the answer first, and never a sentence
that leaves them without a next step.

NUMBER POLICY (shared with shopbot/texts.py): Persian digits everywhere,
EXCEPT amounts and card numbers the customer has to retype into a banking app.
Those stay Latin, because a Persian-digit amount pasted into a bank app is a
failed transfer and a support conversation.
"""

from __future__ import annotations

from typing import Optional

_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa_num(value) -> str:
    """Persian digits, with thousands separators for readability."""
    if isinstance(value, float) and value != int(value):
        text = f"{value:,.2f}".rstrip("0").rstrip(".")
    else:
        text = f"{int(value):,}"
    return text.translate(_PERSIAN_DIGITS)


def money_fa(amount: int) -> str:
    """For amounts the customer only READS. Persian digits."""
    return f"{fa_num(amount)} تومان"


def money_latin(amount: int) -> str:
    """For amounts the customer must TYPE INTO A BANK APP. Latin digits, on
    purpose — see the number policy in this module's docstring."""
    return f"{amount:,} تومان"


def support_line(handle: Optional[str]) -> str:
    """Appended wherever the customer might need a person. Returns an empty
    string when no handle is configured, so callers can always concatenate it
    without producing a dangling 'contact support' that names nobody — an
    instruction the customer cannot follow is worse than no instruction."""
    if not handle:
        return ""
    return f"\n\nسؤالی داشتید: @{handle.lstrip('@')}"


# ── delivery ──────────────────────────────────────────────────────────────
#
# The single most support-generating moment in the product. The customer has
# paid and is now holding a URL that does nothing until they find, install and
# configure an app nobody has named for them. These few lines are the whole
# difference between a completed purchase and a refund request.

def setup_guide(handle: Optional[str]) -> str:
    return (
        "📲 برای اتصال:\n"
        "• اندروید: برنامه v2rayNG\n"
        "• آیفون: برنامه Streisand\n"
        "• ویندوز: برنامه v2rayN\n\n"
        "در برنامه «افزودن اشتراک از لینک» را بزنید و لینک بالا را بچسبانید — "
        "یا همین کد QR را اسکن کنید."
        + support_line(handle)
    )


def delivery_caption(volume_gb: float, days: int, is_trial: bool = False) -> str:
    """Caption on the QR image itself. Kept short — Telegram truncates long
    captions, and the setup guide follows as its own message where it can be
    read without fighting the image for space."""
    if is_trial:
        return (
            f"🎁 سرویس تست شما آماده است\n"
            f"{fa_num(volume_gb)} گیگ — {fa_num(days)} روز\n\n"
            "رایگان است و چیزی از شما کم نشد."
        )
    return f"✅ سرویس شما آماده است\n{fa_num(volume_gb)} گیگ — {fa_num(days)} روز"


def delivery_link(url: str) -> str:
    """The URL sits alone on its own line with nothing after it.

    Not cosmetic: a Latin URL inline with Persian text gets reordered by
    bidirectional rendering, and a customer copying it by hand ends up with a
    mangled link that fails silently in their VPN app.
    """
    return f"🔗 لینک اشتراک شما:\n\n{url}"


# ── payment outcomes ──────────────────────────────────────────────────────

def topup_approved_plain(amount: int, balance: int, handle: Optional[str]) -> str:
    """A wallet top-up with no plan attached — the repeat-customer path."""
    return (
        f"✅ پرداختتان تأیید شد و {money_fa(amount)} به کیف پولتان اضافه شد.\n"
        f"موجودی فعلی: {money_fa(balance)}\n\n"
        "حالا می‌توانید از «🛒 خرید سرویس» ادامه بدهید."
        + support_line(handle)
    )


def topup_approved_with_order(amount: int, balance: int) -> str:
    """The order-first path: the payment is confirmed AND the plan is on its
    way in the same breath. Said before the QR arrives so the customer isn't
    left wondering during the few seconds provisioning takes."""
    return (
        f"✅ پرداختتان تأیید شد — {money_fa(amount)}.\n"
        "دارم سرویس‌تان را می‌سازم، چند لحظه…"
        + (f"\nباقیمانده در کیف پول: {money_fa(balance)}" if balance > 0 else "")
    )


def topup_approved_short(amount: int, balance: int, shortfall: int, handle: Optional[str]) -> str:
    """Approved for less than the plan costs. The money is banked and safe —
    say that first, because the customer's fear is that it vanished."""
    return (
        f"✅ پرداختتان تأیید شد — {money_fa(amount)} به کیف پولتان اضافه شد.\n"
        f"موجودی فعلی: {money_fa(balance)}\n\n"
        f"برای سرویسی که انتخاب کرده بودید {money_fa(shortfall)} کم است. "
        "می‌توانید باقی را شارژ کنید یا سرویس کوچک‌تری بگیرید."
        + support_line(handle)
    )


def topup_rejected(reference: Optional[str], reason: Optional[str], handle: Optional[str]) -> str:
    """Never a bare rejection. The customer believes they sent money; being
    told 'no' with no reason and no way to argue is the worst message in the
    product."""
    head = f"❌ رسید {reference} تأیید نشد." if reference else "❌ رسیدتان تأیید نشد."
    body = f"\nعلت: {reason}" if reason else ""
    return (
        head + body +
        "\n\nاگر فکر می‌کنید اشتباه شده، رسیدتان را دوباره بفرستید یا پیام بدهید."
        + support_line(handle)
    )


# ── the order-first payment request ───────────────────────────────────────

def payment_request(
    volume_gb: float,
    days: int,
    price: int,
    card_number: str,
    card_holder: Optional[str],
    eta_minutes: int,
    from_wallet: int = 0,
) -> str:
    """One exact number, one card, one code.

    The old flow asked the customer to invent an amount before choosing
    anything, then do the multiplication themselves. This asks for a single
    figure they never have to compute, for a thing they have already chosen.

    The amount is in LATIN digits because it is typed into a bank app.
    """
    holder = f"\nبه نام: {card_holder}" if card_holder else ""
    wallet_line = (
        f"\n(از این مبلغ، {money_fa(from_wallet)} از کیف پولتان کم می‌شود)"
        if from_wallet > 0 else ""
    )
    return (
        f"🛒 {fa_num(volume_gb)} گیگ — {fa_num(days)} روزه\n"
        f"مبلغ قابل پرداخت: {money_latin(price)}{wallet_line}\n\n"
        f"این مبلغ را به این کارت واریز کنید:\n"
        f"`{card_number}`{holder}\n\n"
        f"بعد عکس رسید را همینجا بفرستید.\n"
        f"معمولاً تا {fa_num(eta_minutes)} دقیقه بررسی می‌شود و سرویس‌تان خودکار ساخته می‌شود."
    )


# "Receipt received" is NOT here: it is the bot's immediate reply to the
# customer's own action, so it lives in shopbot/handlers/shop.py. The backend
# never sends it, and a second copy would only drift.


# ── expiry ────────────────────────────────────────────────────────────────

def expiring_soon(volume_gb: float, days_left: int, handle: Optional[str]) -> str:
    return (
        f"⏳ سرویس {fa_num(volume_gb)} گیگی‌تان {fa_num(days_left)} روز دیگر تمام می‌شود.\n"
        "اگر نمی‌خواهید قطع شود، از «🛒 خرید سرویس» سرویس بعدی را بگیرید."
        + support_line(handle)
    )


def data_almost_gone(volume_gb: float, percent_used: int, handle: Optional[str]) -> str:
    return (
        f"📉 {fa_num(percent_used)} درصد حجم سرویس {fa_num(volume_gb)} گیگی‌تان مصرف شده.\n"
        "برای اینکه وسط کار قطع نشوید، سرویس بعدی را از قبل بگیرید."
        + support_line(handle)
    )


def trial_ending(hours_left: int, handle: Optional[str]) -> str:
    """The moment a trial pays for itself. The customer has just spent a day
    finding out the service works; this is the one message that asks for the
    sale, and it should arrive while the VPN is still connected rather than
    after it has gone silent on them."""
    return (
        f"⏳ تست رایگانتان تا {fa_num(hours_left)} ساعت دیگر تمام می‌شود.\n"
        "اگر راضی بودید، از «🛒 خرید سرویس» یک سرویس بگیرید تا قطع نشوید."
        + support_line(handle)
    )
