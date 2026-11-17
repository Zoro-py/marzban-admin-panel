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
        # Persian decimal separator «٫»: a «.» inside a Persian line reads as
        # a stray full stop.
        text = f"{value:,.2f}".rstrip("0").rstrip(".").replace(".", "٫")
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


# ── the buy door's labels, mirrored from the bot's menu ──────────────────
#
# Byte-identical to shopbot/texts.py MENU_BUY / MENU_BUY_RENEW (pinned by
# tests/test_shop_texts_copy.py): an instruction may only name a button the
# customer's keyboard actually shows — every account-holder's keyboard
# renders the «/ تمدید» shape, a newcomer's the plain one.

MENU_BUY_LABEL = "🛒 خرید سرویس"
MENU_BUY_RENEW_LABEL = "🛒 خرید / تمدید سرویس"

# The pay-debt-from-wallet refusals the shopbot relays VERBATIM (R1-3):
# customer Persian in the settled vocabulary (T5: «ماندهٔ پرداخت‌نشده» —
# the ledger-speak word for a debt is retired everywhere the customer reads).
# Two distinct causes, two distinct sentences, so a support conversation can
# tell them apart.

PAY_DEBT_NOTHING_TO_PAY = "الان ماندهٔ پرداخت‌نشده‌ای برای پرداخت نیست."
PAY_DEBT_NONE_DETAIL = "ماندهٔ پرداخت‌نشده‌ای ندارید."

# The inline buttons the expiry/usage warning sweeps attach. A real
# account's warning renews THAT account at its own size — the old inline
# label «🔧 تمدید حجم دلخواه» promised a volume choice the tap does not
# offer. The trial's button starts a purchase (its account is not worth
# renewing) and reuses MENU_BUY_LABEL.

WARNING_RENEW_BUTTON = "🔄 تمدید همین سرویس"


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


def delivery_caption(volume_gb: float, days: int, is_trial: bool = False,
                     is_provisional: bool = False, hours: int = 0) -> str:
    """Caption on the QR image itself. Kept short — Telegram truncates long
    captions, and the setup guide follows as its own message where it can be
    read without fighting the image for space."""
    if is_provisional:
        # Said in this order on purpose: connected NOW, the real plan is not
        # lost, and nothing extra is owed. A customer who thinks this small
        # service IS what they paid for would feel cheated on sight.
        return (
            f"⏳ تا تأیید پرداختتان، این سرویس موقت را داشته باشید\n"
            f"{fa_num(volume_gb)} گیگ — {fa_num(hours)} ساعت\n\n"
            "سفارش خودتان سر جایش است و همین لینک "
            "بعد از تأیید، به پلن کامل تبدیل می‌شود."
        )
    if is_trial:
        return (
            f"🎁 سرویس تست شما آماده است\n"
            f"{fa_num(volume_gb)} گیگ — {fa_num(days)} روز\n\n"
            "رایگان است و چیزی از شما کم نشد."
        )
    return f"✅ سرویس شما آماده است\n{fa_num(volume_gb)} گیگ — {fa_num(days)} روز"


def delivery_caption_payg() -> str:
    """Caption on the QR image for a retail pay-as-you-go service (F2).

    A payg start order has price=0 and no fixed volume, so it used to inherit
    the trial caption ("رایگان است و چیزی از شما کم نشد") — which was false
    here in the one direction that matters: usage IS charged, daily, from the
    wallet. This says so up front, before the first meter tick does. No
    volume/day figures on purpose: a payg service has neither."""
    return (
        "📊 سرویس مصرفی‌تان آماده است\n\n"
        "حجم و زمان ثابت ندارد — هر روز فقط به‌اندازهٔ مصرفتان از موجودی‌تان کم می‌شود.\n"
        "اگر موجودی خالی شود سرویس موقتاً قطع می‌شود و با شارژ دوباره، خودش وصل می‌شود."
    )


def order_refunded(amount: int, reason: Optional[str] = None,
                   handle: Optional[str] = None) -> str:
    """A stuck order the sweep refunded on its own (F1) — the customer hears
    it from us, not from a charge that silently reappeared. The old silence
    was indistinguishable from theft: they had paid, the bot went quiet, and
    days later the money was back with nobody saying why. Names the amount
    and that it is back in the wallet, before any why. No blame, no jargon:
    the reason (a restart mid-provisioning) is ours, not theirs."""
    why = f"\nعلت: {reason}" if reason else ""
    return (
        f"سلام — سفارشتان ساخته نشد و {money_fa(amount)} کامل به موجودی‌تان برگشت.\n"
        "هیچ کسری نشده؛ هر وقت خواستید دوباره خرید کنید."
        + why
        + support_line(handle)
    )


def delivery_link(url: str) -> str:
    """The URL sits alone on its own line with nothing after it.

    Not cosmetic: a Latin URL inline with Persian text gets reordered by
    bidirectional rendering, and a customer copying it by hand ends up with a
    mangled link that fails silently in their VPN app.
    """
    return f"🔗 لینک اشتراک شما:\n\n{url}"


# ── payment outcomes ──────────────────────────────────────────────────────

def topup_approved_plain(amount: int, balance: int, handle: Optional[str],
                         has_accounts: bool = False) -> str:
    """A wallet top-up with no plan attached — the repeat-customer path.

    `has_accounts` picks the buy-door label the customer's keyboard actually
    renders (the bot's menu shows «خرید / تمدید سرویس» to an account-holder,
    the plain shape to a newcomer — see MENU_BUY_LABEL above)."""
    label = MENU_BUY_RENEW_LABEL if has_accounts else MENU_BUY_LABEL
    return (
        f"✅ پرداختتان تأیید شد و {money_fa(amount)} به موجودی‌تان اضافه شد.\n"
        f"موجودی فعلی: {money_fa(balance)}\n\n"
        f"حالا می‌توانید از «{label}» ادامه بدهید."
        + support_line(handle)
    )


def topup_approved_with_order(amount: int, balance: int) -> str:
    """The order-first path: the payment is confirmed AND the plan is on its
    way in the same breath. Said before the QR arrives so the customer isn't
    left wondering during the few seconds provisioning takes.

    `balance` here is what is LEFT after the plan was paid, i.e. an
    overpayment. Named as such: a customer who transferred a round 100,000 for
    a 90,000 plan and hears nothing about the other 10,000 concludes the shop
    kept it."""
    extra = (
        f"\n{money_fa(balance)} بیشتر واریز کرده بودید؛ در موجودی‌تان ماند و از خرید بعدی کم می‌شود."
        if balance > 0 else ""
    )
    return (
        f"✅ پرداختتان تأیید شد — {money_fa(amount)}.\n"
        "دارم سرویس‌تان را آماده می‌کنم، چند لحظه…"
        + extra
    )


def topup_approved_no_service(amount: int, balance: int, handle: Optional[str],
                              has_accounts: bool = False) -> str:
    """The payment was fine; building the service was not.

    The money is back in the wallet, so the honest thing is to say both parts
    in that order. Saying only "approved" - which is what used to happen -
    left the customer waiting for a QR that was never coming. `has_accounts`
    picks the buy-door label the keyboard actually renders (same as
    topup_approved_plain)."""
    label = MENU_BUY_RENEW_LABEL if has_accounts else MENU_BUY_LABEL
    return (
        f"✅ پرداختتان تأیید شد — {money_fa(amount)}.\n"
        "اما ساخت سرویس این بار انجام نشد و مبلغ در موجودی‌تان ماند.\n"
        f"موجودی فعلی: {money_fa(balance)}\n\n"
        f"با «{label}» دوباره امتحان کنید؛ چیزی از دست نرفته."
        + support_line(handle)
    )


def topup_approved_short(
    amount: int,
    balance: int,
    shortfall: int,
    card_number: Optional[str],
    card_holder: Optional[str],
    handle: Optional[str],
) -> str:
    """Approved for less than the plan costs.

    Three things, in the order the customer fears them: the money that did
    arrive is safe and theirs; exactly how much more finishes the purchase and
    where to send it; and that they can have it back if they would rather
    stop. The previous wording said only "you can top up the rest" — with no
    card, no amount and no button, which left a newcomer holding money in a
    wallet they had never seen and no way forward.
    """
    card = ""
    if card_number:
        holder = f"\nبه نام: {card_holder}" if card_holder else ""
        card = (
            f"\n\nبرای تکمیل، {money_fa(shortfall)} دیگر به همان کارت واریز کنید:\n"
            f"`{plain_digits(card_number)}`{holder}\n"
            "و عکس رسیدش را همین‌جا بفرستید — سفارشتان نگه داشته شده است."
        )
    return (
        f"✅ {money_fa(amount)} از پرداختتان رسید و در موجودی‌تان محفوظ است.\n"
        f"برای سرویسی که انتخاب کرده بودید {money_fa(shortfall)} کم است."
        + card
        + "\n\nاگر نمی‌خواهید ادامه دهید، پیام بدهید تا مبلغ را برگردانم."
        + support_line(handle)
    )


def topup_rejected(reference: Optional[str], reason: Optional[str], handle: Optional[str]) -> str:
    """Never a bare rejection, and never a reason-less one.

    The customer believes they sent money. A 'no' with no reason is, from
    where they sit, indistinguishable from theft — so when the operator gave
    no reason, the message still names the usual ones and says what to do.
    """
    head = f"❌ رسید {reference} تأیید نشد." if reference else "❌ رسیدتان تأیید نشد."
    if reason:
        body = f"\nعلت: {reason}"
    else:
        body = "\nمعمولاً یعنی تصویر رسید خوانا نبود، یا مبلغ یا شماره کارتِ آن با پرداخت نمی‌خواند."
    tail = (
        f"\n\nاگر واریز کرده‌اید، همین کد را به @{handle.lstrip('@')} بفرستید تا خودم بررسی کنم."
        if handle else
        "\n\nاگر واریز کرده‌اید، رسیدِ واضح‌تری بفرستید."
    )
    return head + body + tail


def provisional_stopped() -> str:
    """Appended to a rejection when a bridge service was running.

    Without it the customer's service simply dies a minute after they are
    told their payment was refused, and they connect the two themselves —
    badly."""
    return (
        f"\n\nسرویس موقتی که داده بودیم هم متوقف شد."
    )


def renewed_in_place(added_gb: float, total_before_gb: Optional[float], total_after_gb: Optional[float],
                     remaining_gb: Optional[float], days_left: Optional[int], handle: Optional[str]) -> str:
    """A renewal on the account the customer already has. The whole message
    is built around two facts: exactly what changed (BEFORE → AFTER, so the
    new total never reads like a brand-new service they didn't buy), and
    that they do not have to do anything — their app already holds this link
    and it simply has more on it now."""
    lines = [f"🎉 {fa_num(added_gb)} گیگ به سرویس‌تان اضافه شد — همین الان فعال است."]
    if total_before_gb is not None and total_after_gb is not None:
        lines.append(f"حجم سرویس‌تان: {fa_num(round(total_before_gb, 2))} گیگ ← {fa_num(round(total_after_gb, 2))} گیگ")
    tail = []
    if remaining_gb is not None:
        tail.append(f"{fa_num(round(remaining_gb, 2))} گیگ باقیمانده")
    if days_left is not None:
        tail.append(f"{fa_num(days_left)} روز")
    elif total_after_gb is not None:
        # A service the operator sold without an expiry keeps having none —
        # silence here would read as "did the expiry disappear?", so say it.
        tail.append("بدون انقضا (بدون تغییر)")
    if tail:
        lines.append("الان: " + " · ".join(tail))
    lines.append("")
    lines.append("همان لینک قبلی کار می‌کند — لازم نیست چیزی را عوض کنید.\n"
                 "اگر برنامه هنوز حجم قبلی را نشان می‌دهد، روی اشتراک بزنید و «به‌روزرسانی» را انتخاب کنید.")
    return "\n".join(lines) + support_line(handle)


def payment_overdue(reference: Optional[str], handle: Optional[str]) -> str:
    """Sent once, when the promised approval time has passed. Owns the delay
    instead of letting silence speak for it."""
    code = f" (کد {reference})" if reference else ""
    return (
        f"⏳ بررسی پرداختتان{code} از زمانی که گفته بودم طولانی‌تر شده — ببخشید.\n"
        "رسیدتان ثبت شده و گم نمی‌شود؛ به محض بررسی همین‌جا خبر می‌دهم."
        + support_line(handle)
    )


def plain_digits(card_number: str) -> str:
    """Card number with nothing but digits, for the tap-to-copy code span.
    Iranian banking apps reject a pasted card number that contains dashes or
    spaces, so the copyable form must be bare even if the display is not."""
    return "".join(ch for ch in card_number if ch.isdigit()) or card_number


# ── expiry ────────────────────────────────────────────────────────────────

def expiring_soon(volume_gb: float, days_left: int, handle: Optional[str]) -> str:
    return (
        f"⏳ سرویس {fa_num(volume_gb)} گیگی‌تان {fa_num(days_left)} روز دیگر تمام می‌شود.\n"
        "اگر نمی‌خواهید قطع شود، از «🛒 خرید / تمدید سرویس» سرویس بعدی را بگیرید."
        + support_line(handle)
    )


def data_almost_gone(volume_gb: float, percent_used: int, handle: Optional[str]) -> str:
    return (
        f"📉 {fa_num(percent_used)} درصد حجم سرویس {fa_num(volume_gb)} گیگی‌تان مصرف شده.\n"
        "برای اینکه وسط کار قطع نشوید، سرویس بعدی را از قبل بگیرید."
        + support_line(handle)
    )


def service_ended(volume_gb: float, reason: str, handle: Optional[str]) -> str:
    """The moment the service actually dies. The 3-day and 80% warnings fire
    BEFORE death by design, and nothing used to fire AT death — the customer
    discovered the outage by their VPN failing and had to open the bot to
    learn why (2026-11-18, owner GO). One calm push with the renew button
    riding on it closes the reminder chain: reason first, then the one-tap
    way back, then the link reassurance (renewal in place)."""
    return (
        f"🔴 سرویس {fa_num(volume_gb)} گیگی‌تان قطع شد — {reason}\n"
        "دکمهٔ «تمدید» پایین، یک تپ دوباره وصلش می‌کند — لینک‌تان بعد از تمدید همان می‌ماند."
        + support_line(handle)
    )


def trial_ended(handle: Optional[str]) -> str:
    """The free trial just expired. The 2-hour nudge already made the ask;
    this is the death moment — one line, the offer sitting on the button
    below, no second deadline."""
    return (
        "🔴 تست رایگانتان تمام شد.\n"
        "اگر راضی بودید، دکمهٔ پایین با کمترین پلنِ واقعی همین سرویس را دوباره وصل می‌کند."
        + support_line(handle)
    )


# ── bounded-debt auto-renew ("⚡ تمدید خودکار") ─────────────────────────────
#
# The TAP's own reply (success / already-in-debt / no-history) is rendered
# by shopbot itself (shopbot/texts.py) from the backend's structured JSON —
# same pattern as every other synchronous button action in this shop. Only
# the ASYNC pushes below (no live conversation to reply into — the debt
# sweep runs server-side, minutes to days after the tap) go through this
# module and send_to_shop_user, same as the usage/expiry warnings above.


def debt_reminder(owed: int, days_left: int) -> str:
    return (
        f"⏳ یادآوری: ماندهٔ پرداخت‌نشدهٔ شما {money_fa(owed)} است.\n"
        f"تا {fa_num(max(days_left, 0))} روز دیگر برای شارژ وقت دارید، وگرنه سرویس موقتاً متوقف می‌شود."
    )


def debt_suspended(owed: int) -> str:
    return (
        f"⛔ سرویستان به‌دلیل ماندهٔ پرداخت‌نشدهٔ {money_fa(owed)} موقتاً متوقف شد.\n"
        "به محض شارژ، خودش دوباره فعال می‌شود — لازم نیست با پشتیبانی تماس بگیرید."
    )


def debt_cleared_reactivated() -> str:
    return "✅ موجودی‌تان شارژ شد و سرویستان دوباره فعال است."


# ── retail pay-as-you-go ("📊 سرویس مصرفی") daily meter ────────────────────

def payg_low_balance(balance: int) -> str:
    return (
        f"⏳ موجودی‌تان {money_fa(balance)} است و رو به اتمام.\n"
        "برای اینکه سرویستان قطع نشود، شارژ کنید."
    )


def payg_suspended(owed: int) -> str:
    return (
        f"⛔ موجودی‌تان کافی نبود و سرویس مصرفی‌تان موقتاً متوقف شد "
        f"({money_fa(owed)} کم آمد).\n"
        "به محض شارژ، خودش دوباره فعال می‌شود."
    )


def payg_reactivated() -> str:
    return "✅ موجودی‌تان شارژ شد و سرویس مصرفی‌تان دوباره فعال است."


def trial_ending(hours_left: int, handle: Optional[str]) -> str:
    """The moment a trial pays for itself. The customer has just spent a day
    finding out the service works; this is the one message that asks for the
    sale, and it should arrive while the VPN is still connected rather than
    after it has gone silent on them."""
    return (
        f"⏳ تست رایگانتان تا {fa_num(hours_left)} ساعت دیگر تمام می‌شود.\n"
        "اگر راضی بودید، از «🛒 خرید / تمدید سرویس» یک سرویس بگیرید تا قطع نشوید."
        + support_line(handle)
    )
