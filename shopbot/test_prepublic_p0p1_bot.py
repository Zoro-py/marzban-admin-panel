"""Regression tests for the prepublic P0+P1 bot cluster (2026-10-23):

  * F10 — a shortfall UNDER the shop's min_topup asks for the minimum and
    promises the surplus back to the wallet; a shortfall above it keeps the
    exact-amount ask; the receipt is submitted with the ASKED amount.
  * F3(b) — the backend's 409 PAYG_TOPUP_INSTEAD refusal maps to its own
    Persian text (and SERVICE_IS_UNLIMITED keeps its existing one).
  * F12 — a 502 on the instant wallet purchase (the backend refunds before
    answering) maps to the refund-stated text; other failures stay generic.
  * F11 — account_line renders the suspended badge only when a suspended
    status is passed; every other rendering stays byte-identical.
  * Ride-along — payg meter wallet entries label «بیل مصرف روزانه», keyed off
    the backend's note, not the shared "purchase" entry type.

Telegram objects and the backend client are stubbed — nothing real is
touched. Same script style as test_shop_link_claim.py.

Run (from shopbot/, using bot's venv — same interpreter family):
  ../bot/venv/Scripts/python test_prepublic_p0p1_bot.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("SHOP_BOT_TOKEN", "")
os.environ.setdefault("SHOP_BOT_API_KEY", "k")
os.environ.setdefault("API_BASE_URL", "https://panel.test")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import texts  # noqa: E402
import handlers.shop as shop  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


class StubMessage:
    def __init__(self):
        self.replies: list[tuple] = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append((text, reply_markup))


def make_update(user_id: int = 555):
    msg = StubMessage()
    upd = SimpleNamespace(
        effective_message=msg,
        effective_user=SimpleNamespace(id=user_id, username="ali_t"),
    )
    context = SimpleNamespace(args=[], user_data={})
    return (upd, context), msg


class StubBackend:
    """Returns the order intent on POST /orders; captures the topup POST;
    raises on demand for the error-mapping cases."""

    def __init__(self):
        self.order_intent: dict = {}
        self.order_error: tuple[str, int] | None = None
        self.topup_posts: list[dict] = []
        self.pay_error: tuple[str, int] | None = None

    async def post(self, path: str, json=None, timeout: float = 20):
        if path == "/api/shop/bot/orders":
            if self.order_error is not None:
                raise shop.ShopApiError(self.order_error[0], status=self.order_error[1])
            return dict(self.order_intent)
        if path == "/api/shop/bot/topups":
            self.topup_posts.append(json or {})
            return {"reference_code": "ABCD", "status": "pending"}
        if path.startswith("/api/shop/bot/orders/") and path.endswith("/pay"):
            if self.pay_error is not None:
                raise shop.ShopApiError(self.pay_error[0], status=self.pay_error[1])
            return {"order_id": 1, "status": "delivered"}
        raise AssertionError(path)


def base_session() -> dict:
    return {
        "balance": 0, "trial_available": False, "shop_name": None, "support_handle": None,
        "is_blocked": False, "is_open": True,
        "min_topup": 50_000, "max_topup": 5_000_000, "card_number": "6037-9911-2222-3333",
        "card_holder": None, "approval_eta_minutes": 30,
        "price_per_gb": 10_000, "min_gb": 5, "max_gb": 100, "plan_duration_days": 30,
    }


SESSION = base_session()


async def fake_session(update) -> dict:
    return dict(SESSION)


stub = StubBackend()
shop.backend = stub
shop._session = fake_session  # type: ignore[assignment]


def order_intent(**overrides) -> dict:
    intent = {
        "order_id": 77, "data_limit_gb": 10, "duration_days": 30,
        "price": 100_000, "balance": 0, "shortfall": 100_000,
        "payable_from_wallet": False, "card_number": "6037-9911-2222-3333",
        "card_holder": None, "approval_eta_minutes": 30,
    }
    intent.update(overrides)
    return intent


async def main() -> None:
    # ── F10: shortfall < min_topup ────────────────────────────────────────
    stub.order_intent = order_intent(shortfall=30_000)
    (upd, context), msg = make_update()
    await shop._handle_volume(upd, context, 10)
    # F-E: the optional-name step sits between the volume and the payment
    # screen — an empty text answers it as "no name", continuing the flow on
    # the SAME context (the stashed volume/state live there).
    msg.text = ""
    upd.message = msg  # handle_text reads update.message.text
    await shop.handle_text(upd, context)
    check("sub-min shortfall asks for min_topup, not the shortfall",
          context.user_data[shop._PENDING_AMOUNT] == 50_000
          and context.user_data[shop._STATE] == shop._STATE_AWAITING_RECEIPT)
    text = msg.replies[-1][0]
    check("sub-min text carries the minimum AND the surplus promise",
          "حداقل" in text and "مازاد بر بدهی به کیف پولتان برمی‌گردد" in text
          and "۵۰,۰۰۰" in text)
    check("sub-min text still names the actual debt (۳۰,۰۰۰)", "۳۰,۰۰۰" in text)
    check("sub-min text has the Latin copy span for the bank app",
          "`50000`" in text and "`30000`" not in text)
    check("sub-min order still carries the order id for the bound receipt",
          context.user_data[shop._ORDER_ID] == 77)

    # The receipt goes out with the ASKED amount, bound to the order.
    await shop._submit_receipt(upd, context, receipt_file_id="photo")
    check("receipt POSTed with claimed_amount=min_topup and the order id",
          stub.topup_posts[-1]["claimed_amount"] == 50_000
          and stub.topup_posts[-1]["order_id"] == 77)

    # ── F10: shortfall > min_topup keeps the exact-amount ask ────────────
    stub.order_intent = order_intent(shortfall=120_000)
    (upd, context), msg = make_update()
    await shop._handle_volume(upd, context, 10)
    # F-E: the optional-name step sits between the volume and the payment
    # screen — an empty text answers it as "no name", continuing the flow on
    # the SAME context (the stashed volume/state live there).
    msg.text = ""
    upd.message = msg  # handle_text reads update.message.text
    await shop.handle_text(upd, context)
    check("above-min shortfall asks exactly the shortfall",
          context.user_data[shop._PENDING_AMOUNT] == 120_000)
    text = msg.replies[-1][0]
    check("above-min text has no minimum/surplus wording",
          "حداقل" not in text and "مازاد بر بدهی" not in text
          and "برای تکمیل، مبلغ" in text)

    # ── F10: min_topup = 0 means "no minimum" — shortfall unchanged ──────
    SESSION["min_topup"] = 0
    stub.order_intent = order_intent(shortfall=30_000)
    (upd, context), msg = make_update()
    await shop._handle_volume(upd, context, 10)
    # F-E: the optional-name step sits between the volume and the payment
    # screen — an empty text answers it as "no name", continuing the flow on
    # the SAME context (the stashed volume/state live there).
    msg.text = ""
    upd.message = msg  # handle_text reads update.message.text
    await shop.handle_text(upd, context)
    check("min_topup=0 asks the bare shortfall as before",
          context.user_data[shop._PENDING_AMOUNT] == 30_000
          and "برای تکمیل، مبلغ" in msg.replies[-1][0])
    SESSION.update(base_session())

    # ── F3(b): the PAYG_TOPUP_INSTEAD 409 ─────────────────────────────────
    stub.order_error = ("PAYG_TOPUP_INSTEAD: current service is metered", 409)
    (upd, context), msg = make_update()
    await shop._handle_volume(upd, context, 10)
    # F-E: the optional-name step sits between the volume and the payment
    # screen — an empty text answers it as "no name", continuing the flow on
    # the SAME context (the stashed volume/state live there).
    msg.text = ""
    upd.message = msg  # handle_text reads update.message.text
    await shop.handle_text(upd, context)
    check("PAYG_TOPUP_INSTEAD maps to its own text",
          msg.replies[-1][0] == texts.PAYG_TOPUP_INSTEAD)
    check("PAYG_TOPUP_INSTEAD clears the flow state", context.user_data == {})
    check("PAYG_TOPUP_INSTEAD text says no money moved",
          "پولی از کیف پولتان کم نشد" in texts.PAYG_TOPUP_INSTEAD)

    # SERVICE_IS_UNLIMITED keeps its existing mapping (regression).
    stub.order_error = ("SERVICE_IS_UNLIMITED: renewal target has no cap", 409)
    (upd, context), msg = make_update()
    await shop._handle_volume(upd, context, 10)
    # F-E: the optional-name step sits between the volume and the payment
    # screen — an empty text answers it as "no name", continuing the flow on
    # the SAME context (the stashed volume/state live there).
    msg.text = ""
    upd.message = msg  # handle_text reads update.message.text
    await shop.handle_text(upd, context)
    check("SERVICE_IS_UNLIMITED still maps to SERVICE_UNLIMITED",
          msg.replies[-1][0] == texts.SERVICE_UNLIMITED)
    stub.order_error = None

    # ── F12: 502 on the instant purchase → refund-stated text ────────────
    stub.pay_error = ("Couldn't create the account, so nothing was charged", 502)
    (upd, context), msg = make_update()
    context.user_data[shop._ORDER_ID] = 77
    await shop._confirm_wallet_purchase(upd, context)
    check("502 on pay maps to PURCHASE_FAILED_REFUNDED",
          any(r[0] == texts.PURCHASE_FAILED_REFUNDED for r in msg.replies))
    check("refund text states the money came back",
          "به کیف پولتان برگشت" in texts.PURCHASE_FAILED_REFUNDED
          and "موجودی‌تان سالم است" in texts.PURCHASE_FAILED_REFUNDED)

    # Any other failure keeps the generic error.
    stub.pay_error = ("boom", 500)
    (upd, context), msg = make_update()
    context.user_data[shop._ORDER_ID] = 77
    await shop._confirm_wallet_purchase(upd, context)
    check("non-502 pay failure keeps generic_error",
          any(r[0] == texts.generic_error(None) for r in msg.replies)
          and all(r[0] != texts.PURCHASE_FAILED_REFUNDED for r in msg.replies))
    stub.pay_error = None

    # ── F11: account_line suspended badge on/off ──────────────────────────
    plain = texts.account_line(10.0, 2.5, 20)
    check("no status passed → rendering unchanged (exact legacy string)",
          plain == "🔑 سرویس ۱۰ گیگ\n"
                   "▰▰▱▱▱▱▱▱▱▱ ۷٫۵ گیگ باقی مانده (۲۵٪ مصرف شده)\n"
                   "۲۰ روز مانده", repr(plain))
    with_badge = texts.account_line(10.0, 2.5, 20, status="disabled")
    check("disabled status appends exactly the badge line",
          with_badge == plain + "\n" + texts.STATUS_SUSPENDED_BADGE)
    check("active/limited/expired statuses render without a badge",
          all(texts.account_line(10.0, 2.5, 20, status=s) == plain
              for s in ("active", "limited", "expired", None)))
    unlimited = texts.account_line(None, 3.0, None)
    unlimited_badge = texts.account_line(None, 3.0, None, status="disabled")
    check("unlimited + disabled still gets the badge",
          unlimited_badge == unlimited + "\n" + texts.STATUS_SUSPENDED_BADGE
          and "نامحدود" in unlimited_badge)

    # ── Ride-along: the payg meter wallet label ───────────────────────────
    check("english meter note → بیل مصرف روزانه",
          shop._wallet_label({"type": "purchase", "note": "Payg meter: 0.123 GB"})
          == "بیل مصرف روزانه")
    check("persian meter note (backend's relabel) → بیل مصرف روزانه",
          shop._wallet_label({"type": "purchase", "note": "بیل مصرف روزانه: 0.123 GB"})
          == "بیل مصرف روزانه")
    check("a real purchase keeps خرید سرویس",
          shop._wallet_label({"type": "purchase", "note": None}) == "خرید سرویس"
          and shop._wallet_label({"type": "purchase", "note": "order #12"}) == "خرید سرویس")
    check("other entry types keep their labels",
          shop._wallet_label({"type": "topup", "note": None}) == "شارژ کیف پول"
          and shop._wallet_label({"type": "refund", "note": None}) == "بازگشت وجه"
          and shop._wallet_label({"type": "unknown", "note": None}) == "تراکنش")

    # End-to-end: a meter entry renders through show_wallet's line builder.
    SESSION["balance"] = -5_000
    (upd, context), msg = make_update()

    async def wallet_session(update) -> dict:
        return dict(SESSION)

    shop._session = wallet_session  # type: ignore[assignment]

    async def fake_get(path, params=None):
        if path == "/api/shop/bot/wallet":
            return [
                {"type": "purchase", "amount": -1234, "note": "Payg meter: 0.123 GB"},
                {"type": "purchase", "amount": -100_000, "note": None},
            ]
        raise AssertionError(path)

    shop.backend = SimpleNamespace(post=stub.post, get=fake_get)  # type: ignore[assignment]
    await shop.show_wallet(upd, context)
    wallet_text = msg.replies[-1][0]
    check("meter entry renders as بیل مصرف روزانه in the wallet screen",
          "بیل مصرف روزانه: ۱,۲۳۴ تومان" in wallet_text)
    check("plain purchase on the same screen still reads خرید سرویس",
          "خرید سرویس: ۱۰۰,۰۰۰ تومان" in wallet_text)


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All shopbot prepublic P0+P1 bot-cluster cases passed.")
