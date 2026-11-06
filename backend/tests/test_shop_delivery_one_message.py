"""T4: the shop's DELIVERY is one message — QR photo whose caption carries
the plan line, the subscription link (own line, copy-safe), and the setup
guide, with the link ALSO as an inline URL button on the photo.

The old shape sent THREE messages (photo / link / guide); the middle of a
purchase was where customers got lost. The photo-failure fallback still
sends the link + guide as text — a customer who can copy a URL is not
blocked by a missing image.

Plain `python -m tests.test_shop_delivery_one_message` from `backend/` —
same harness shape as tests/test_shop.py.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="shop_deliv_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""
os.environ["SHOP_BOT_API_KEY"] = "test-shop-key"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    BillingMode,
    ShopOrder,
    ShopOrderStatus,
    ShopUser,
    utcnow,
)
from app import notify as notify_module  # noqa: E402
from app.shop_service import deliver_order_to_customer  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


def make_order(session: Session, *, extends: bool = False) -> ShopOrder:
    from sqlmodel import select
    user = session.exec(select(ShopUser).where(ShopUser.telegram_id == 4242)).first()
    if user is None:
        user = ShopUser(telegram_id=4242, username="deliv_t")
        session.add(user)
        session.commit()
        session.refresh(user)
    tag = "renew" if extends else "one"
    account = Account(
        marzban_username=f"shop777-{tag}",
        used_traffic=1024 ** 3,
        data_limit=30 * 1024 ** 3,
        subscription_url=f"https://sub.example.com/s/deliv{tag}",
        billing_mode=BillingMode.prepay,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    order = ShopOrder(
        shop_user_id=user.id,
        data_limit_gb=30,
        duration_days=30,
        price=300_000,
        status=ShopOrderStatus.delivered,
        account_id=account.id,
        marzban_username=f"shop777-{tag}",
        delivered_at=utcnow(),
        extends_account_id=account.id if extends else None,
    )
    session.add(order)
    session.commit()
    session.refresh(order)
    return order


async def main_async() -> None:
    init_db()
    with Session(engine) as session:
        order = make_order(session)

        photos: list[dict] = []
        texts: list[str] = []

        async def capture_photo(chat_id, photo, caption, *, filename="qr.png", reply_markup=None):
            photos.append({"caption": caption, "reply_markup": reply_markup})

        async def capture_text(chat_id, text):
            texts.append(text)

        original_photo = notify_module.send_photo_to_shop_user
        original_text = notify_module.send_to_shop_user
        try:
            notify_module.send_photo_to_shop_user = capture_photo
            notify_module.send_to_shop_user = capture_text

            ok = await deliver_order_to_customer(session, order)
            check("delivery reports success", ok is True)
            check("ONE photo message, ZERO extra text messages",
                  len(photos) == 1 and not texts, repr((len(photos), texts)))

            caption = photos[0]["caption"]
            markup = photos[0]["reply_markup"]
            check("caption carries the plan line (آماده است + گیگ)",
                  "آماده است" in caption and "۳۰" in caption, repr(caption))
            check("caption carries the link on its own line (copy-safe)",
                  "https://sub.example.com/s/delivone" in caption, repr(caption))
            check("caption carries the setup guide (v2rayNG named)",
                  "v2rayNG" in caption, repr(caption))
            check("caption fits Telegram's photo-caption limit",
                  len(caption) <= 1024, repr(len(caption)))
            check("reply_markup is the inline URL button for the SAME link",
                  isinstance(markup, dict)
                  and markup.get("inline_keyboard") == [[{"text": "🔗 لینک اشتراک",
                                                          "url": "https://sub.example.com/s/delivone"}]],
                  repr(markup))

            # ── the QR failing must NOT strand the customer ────────────────
            photos.clear()
            texts.clear()

            async def broken_photo(chat_id, photo, caption, *, filename="qr.png", reply_markup=None):
                raise RuntimeError("telegram down")

            notify_module.send_photo_to_shop_user = broken_photo
            ok = await deliver_order_to_customer(session, order)
            check("photo failure still delivers the link + guide as text",
                  ok is True and len(texts) == 2
                  and any("sub.example.com" in t for t in texts)
                  and any("v2rayNG" in t for t in texts), repr((ok, texts)))

            # ── renewal-in-place keeps its own single text message ────────
            photos.clear()
            texts.clear()
            notify_module.send_photo_to_shop_user = capture_photo
            renewal = make_order(session, extends=True)
            ok = await deliver_order_to_customer(session, renewal)
            check("renewal-in-place: one text, no photo",
                  ok is True and not photos and len(texts) == 1
                  and "اضافه شد" in texts[0], repr((len(photos), texts)))
        finally:
            notify_module.send_photo_to_shop_user = original_photo
            notify_module.send_to_shop_user = original_text


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        sys.exit(1)
    print("All shop delivery one-message cases passed.")
