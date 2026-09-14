"""Self-serve shop API.

TWO ROUTERS, TWO DIFFERENT AUTH BOUNDARIES — the whole point of this file's
shape:

  router      /api/shop/*      operator only, same JWT as the rest of the
                               dashboard. Reads every wallet, approves money.
  bot_router  /api/shop/bot/*  the customer-facing shop bot, holding ONLY the
                               SHOP_BOT_API_KEY shared secret.

The shop bot takes messages from the public, so it is the component most
likely to be compromised. It therefore gets a key that reaches nothing but
these endpoints — not settlements, not backups, not the reseller ledger. Do
not "simplify" this by putting the bot endpoints behind require_auth and
handing the bot the Marzban admin credentials; that is precisely the
consolidation this split exists to prevent.

Endpoints identify the customer by telegram_id — in the body for the POSTs,
as a query parameter for /accounts and /wallet. The one exception is
/purchase/{order_id}/deliver, which names no customer at all and acts on
whoever owns that order; re-sending a QR creates nothing and charges nothing,
so the order id is sufficient authority there.

The key authenticates the bot as a whole, not the individual customer — the
bot is trusted to report who is talking to it, exactly as it is trusted to
report what they asked for. That trust is bounded: a compromised shop bot can
spend its own customers' wallets, which is bad, but it can neither create
money (only an operator approval does that) nor touch anything outside /shop.
"""

from __future__ import annotations

import logging
import secrets
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from sqlmodel import Session, select

from app.auth import require_auth
from app.bulk_accounts import build_caption, format_plan_line, resolve_subscription_url
from app.config import settings as app_settings
from app.db import get_session
from app import shop_texts
from app.models import (
    Account,
    Customer,
    ShopOrder,
    ShopOrderStatus,
    ShopTopup,
    ShopTopupStatus,
    ShopUser,
    ShopWalletEntry,
    ShopWalletEntryType,
)
from app.notify import (
    relay_shop_photo_to_admin,
    notify_admin,
    notify_admin_with_buttons,
    send_photo_to_shop_user,
    send_to_shop_user,
)
from app.qr import subscription_qr_png
from app.schemas import (
    ShopBotAccountRow,
    ShopBotOrderAction,
    ShopBotPurchaseRequest,
    ShopBotSession,
    ShopBotSessionRequest,
    ShopBotTopupRequest,
    ShopOrderIntent,
    ShopOrderRead,
    ShopPurchaseResult,
    ShopSettingsRead,
    ShopSettingsUpdate,
    ShopTopupDecision,
    ShopTopupRead,
    ShopUserRead,
    ShopUserUpdate,
    ShopWalletAdjustRequest,
    ShopWalletEntryRead,
)
from app.shop_service import (
    ShopError,
    approve_topup,
    create_awaiting_order,
    deliver_order_to_customer,
    grant_trial,
    find_topup_by_code,
    is_existing_customer,
    latest_awaiting_order,
    pay_awaiting_order,
    create_topup,
    get_or_create_shop_user,
    get_shop_settings,
    post_wallet_entry,
    purchase,
    quote_price,
    reject_topup,
    validate_purchase_request,
    wallet_balance,
)

logger = logging.getLogger(__name__)


def require_shop_bot(x_shop_bot_key: Optional[str] = Header(default=None)) -> None:
    """Fails CLOSED when SHOP_BOT_API_KEY is unset.

    An unset key means the operator hasn't configured the shop bot, and the
    safe reading of that is "this surface isn't in use," not "let anyone in."
    Compared with compare_digest rather than `==` so the comparison doesn't
    leak the key one byte at a time through response timing.
    """
    expected = app_settings.shop_bot_api_key
    if not expected:
        raise HTTPException(503, "Shop bot API is not configured on this server")
    if not x_shop_bot_key:
        raise HTTPException(401, "Invalid shop bot key")
    # Compared as BYTES. compare_digest refuses str operands containing any
    # non-ASCII character, and uvicorn hands header values through as latin-1
    # — so a key header with any byte >= 0x80 raised TypeError out of the auth
    # dependency and returned 500 instead of 401. Encoding first makes every
    # rejection look the same, which is also the point of using it at all.
    if not secrets.compare_digest(x_shop_bot_key.encode("utf-8", "surrogateescape"),
                                  expected.encode("utf-8")):
        raise HTTPException(401, "Invalid shop bot key")


router = APIRouter(prefix="/api/shop", tags=["shop"], dependencies=[Depends(require_auth)])
# The guard lives on the ROUTER, not on each route, deliberately. Declared
# per-route it has to be remembered every time an endpoint is added here, and
# forgetting it produces an unauthenticated endpoint that looks exactly like
# its guarded neighbours in the file — silent, and on the one surface in this
# codebase that faces the public. On the router it is the default and an
# omission is impossible rather than merely unlikely.
bot_router = APIRouter(
    prefix="/api/shop/bot", tags=["shop-bot"], dependencies=[Depends(require_shop_bot)]
)


# ══════════════════════════════════════════════════ operator-facing endpoints


def _shop_user_row(session: Session, user: ShopUser) -> ShopUserRead:
    return ShopUserRead(
        id=user.id,
        telegram_id=user.telegram_id,
        telegram_username=user.telegram_username,
        display_name=user.display_name,
        phone=user.phone,
        customer_id=user.customer_id,
        is_blocked=user.is_blocked,
        balance=wallet_balance(session, user.id),
        created_at=user.created_at,
        last_seen_at=user.last_seen_at,
    )


@router.get("/settings", response_model=ShopSettingsRead)
def read_settings(session: Session = Depends(get_session)):
    return get_shop_settings(session)


@router.patch("/settings", response_model=ShopSettingsRead)
def update_settings(body: ShopSettingsUpdate, session: Session = Depends(get_session)):
    settings = get_shop_settings(session)
    updates = body.model_dump(exclude_unset=True)

    # Opening the shop with no price or no card number would show customers a
    # working buy button that cannot complete, and a top-up screen with
    # nowhere to send money. Refuse the combination rather than let the shop
    # go live broken.
    merged_open = updates.get("is_open", settings.is_open)
    merged_price = updates.get("price_per_gb", settings.price_per_gb)
    merged_card = updates.get("card_number", settings.card_number)
    if merged_open:
        if not merged_price or merged_price <= 0:
            raise HTTPException(400, "Set a price per GB before opening the shop")
        if not (merged_card or "").strip():
            raise HTTPException(400, "Set a card number before opening the shop — customers need somewhere to pay")

    merged_min = updates.get("min_gb", settings.min_gb)
    merged_max = updates.get("max_gb", settings.max_gb)
    if merged_min > merged_max:
        raise HTTPException(400, "min_gb cannot be greater than max_gb")
    if updates.get("min_topup", settings.min_topup) > updates.get("max_topup", settings.max_topup):
        raise HTTPException(400, "min_topup cannot be greater than max_topup")

    # An operator will type "@myshop" as often as "myshop". Stripping here
    # rather than at every read means the bot never ships "@@myshop" in a
    # message to a customer, and there is one place that decides the form.
    if "support_handle" in updates and updates["support_handle"]:
        updates["support_handle"] = updates["support_handle"].strip().lstrip("@") or None

    for field, value in updates.items():
        setattr(settings, field, value)
    session.add(settings)
    session.commit()
    session.refresh(settings)
    return settings


@router.get("/users", response_model=list[ShopUserRead])
def list_shop_users(session: Session = Depends(get_session)):
    users = session.exec(select(ShopUser).order_by(ShopUser.created_at.desc())).all()
    return [_shop_user_row(session, u) for u in users]


@router.patch("/users/{user_id}", response_model=ShopUserRead)
def update_shop_user(user_id: int, body: ShopUserUpdate, session: Session = Depends(get_session)):
    user = session.get(ShopUser, user_id)
    if not user:
        raise HTTPException(404, "Shop user not found")
    updates = body.model_dump(exclude_unset=True)
    if updates.get("customer_id") is not None and not session.get(Customer, updates["customer_id"]):
        raise HTTPException(404, "customer_id not found")
    for field, value in updates.items():
        setattr(user, field, value)
    session.add(user)
    session.commit()
    session.refresh(user)
    return _shop_user_row(session, user)


@router.get("/users/{user_id}/wallet", response_model=list[ShopWalletEntryRead])
def list_wallet_entries(user_id: int, limit: int = 100, session: Session = Depends(get_session)):
    if not session.get(ShopUser, user_id):
        raise HTTPException(404, "Shop user not found")
    return session.exec(
        select(ShopWalletEntry)
        .where(ShopWalletEntry.shop_user_id == user_id)
        .order_by(ShopWalletEntry.created_at.desc())
        .limit(limit)
    ).all()


@router.post("/users/{user_id}/wallet", response_model=ShopUserRead)
def adjust_wallet(user_id: int, body: ShopWalletAdjustRequest, session: Session = Depends(get_session)):
    """Manual correction. Signed, and always an APPENDED entry — never an edit
    of an existing row, so the reason a balance changed stays readable."""
    user = session.get(ShopUser, user_id)
    if not user:
        raise HTTPException(404, "Shop user not found")
    if body.amount == 0:
        raise HTTPException(400, "amount must not be zero")
    post_wallet_entry(
        session,
        user_id,
        entry_type=ShopWalletEntryType.adjust,
        amount=body.amount,
        note=body.note or "Manual adjustment by operator",
    )
    return _shop_user_row(session, user)


@router.get("/topups", response_model=list[ShopTopupRead])
def list_topups(
    status: Optional[ShopTopupStatus] = None,
    limit: int = 100,
    session: Session = Depends(get_session),
):
    stmt = select(ShopTopup)
    if status is not None:
        stmt = stmt.where(ShopTopup.status == status)
    topups = session.exec(stmt.order_by(ShopTopup.created_at.desc()).limit(limit)).all()

    users = {u.id: u for u in session.exec(select(ShopUser)).all()}
    return [
        ShopTopupRead(
            **topup.model_dump(),
            telegram_id=users[topup.shop_user_id].telegram_id if topup.shop_user_id in users else None,
            display_name=users[topup.shop_user_id].display_name if topup.shop_user_id in users else None,
        )
        for topup in topups
    ]


@router.post("/topups/{topup_id}/approve", response_model=ShopTopupRead)
async def approve_topup_endpoint(
    topup_id: int,
    body: ShopTopupDecision = ShopTopupDecision(),
    session: Session = Depends(get_session),
):
    topup = session.get(ShopTopup, topup_id)
    if not topup:
        raise HTTPException(404, "Top-up not found")
    try:
        topup, delivered_order = await approve_topup(session, topup, approved_amount=body.amount)
    except ShopError as exc:
        raise HTTPException(400, str(exc))

    user = session.get(ShopUser, topup.shop_user_id)
    balance = wallet_balance(session, topup.shop_user_id)
    shop_settings = get_shop_settings(session)
    # Best-effort throughout: the money is already credited and committed. A
    # customer who doesn't get the message can still see the new balance in
    # the bot, so failing the request here would undo nothing and only confuse
    # the operator into approving twice.
    if user is not None:
        try:
            if delivered_order is not None and delivered_order.status == ShopOrderStatus.delivered:
                # The order-first path: this payment was sent FOR a plan, and
                # that plan has just been paid and provisioned. Confirm the
                # money BEFORE the QR arrives so the few seconds of
                # provisioning aren't silence.
                await send_to_shop_user(
                    user.telegram_id,
                    shop_texts.topup_approved_with_order(topup.approved_amount, balance),
                )
                await deliver_order_to_customer(session, delivered_order)
            elif topup.order_id is not None:
                # Bound to a plan that was not delivered. WHICH message to send
                # depends on why, and getting that wrong is worse than saying
                # nothing: a customer whose service failed and got refunded was
                # being told an amount was still missing, and one whose plan was
                # already active was told the same.
                pending = session.get(ShopOrder, topup.order_id)
                pending_status = pending.status if pending else None
                if pending_status == ShopOrderStatus.awaiting_payment:
                    # Almost always: the operator approved less than it costs.
                    shortfall = max(0, (pending.price if pending else 0) - balance)
                    await send_to_shop_user(
                        user.telegram_id,
                        shop_texts.topup_approved_short(
                            topup.approved_amount, balance, shortfall,
                            shop_settings.card_number, shop_settings.card_holder,
                            shop_settings.support_handle,
                        ),
                    )
                elif pending_status == ShopOrderStatus.failed:
                    # Paid, then the panel refused. The refund is already in the
                    # wallet; say so rather than promising a subscription.
                    await send_to_shop_user(
                        user.telegram_id,
                        shop_texts.topup_approved_no_service(
                            topup.approved_amount, balance, shop_settings.support_handle,
                        ),
                    )
                else:
                    # Already delivered by another route (a wallet payment that
                    # beat the receipt, say). The credit is simply credit.
                    await send_to_shop_user(
                        user.telegram_id,
                        shop_texts.topup_approved_plain(
                            topup.approved_amount, balance, shop_settings.support_handle,
                        ),
                    )
            else:
                await send_to_shop_user(
                    user.telegram_id,
                    shop_texts.topup_approved_plain(
                        topup.approved_amount, balance, shop_settings.support_handle,
                    ),
                )
        except Exception:
            logger.exception("Top-up #%s approved but the customer could not be notified", topup.id)

    return ShopTopupRead(**topup.model_dump(), telegram_id=user.telegram_id if user else None,
                         display_name=user.display_name if user else None)


@router.post("/topups/{topup_id}/reject", response_model=ShopTopupRead)
async def reject_topup_endpoint(
    topup_id: int,
    body: ShopTopupDecision = ShopTopupDecision(),
    session: Session = Depends(get_session),
):
    topup = session.get(ShopTopup, topup_id)
    if not topup:
        raise HTTPException(404, "Top-up not found")
    try:
        topup = reject_topup(session, topup, reason=body.reason)
    except ShopError as exc:
        raise HTTPException(400, str(exc))

    user = session.get(ShopUser, topup.shop_user_id)
    if user is not None:
        # Through shop_texts so a rejection is never reason-less: from where the
        # customer sits, a bare "no" after sending money to a personal card is
        # indistinguishable from theft.
        try:
            await send_to_shop_user(
                user.telegram_id,
                shop_texts.topup_rejected(
                    topup.reference_code, topup.reject_reason,
                    get_shop_settings(session).support_handle,
                ),
            )
        except Exception:
            logger.exception("Top-up #%s rejected but the customer could not be notified", topup.id)

    return ShopTopupRead(**topup.model_dump(), telegram_id=user.telegram_id if user else None,
                         display_name=user.display_name if user else None)


@router.get("/orders", response_model=list[ShopOrderRead])
def list_orders(limit: int = 100, session: Session = Depends(get_session)):
    orders = session.exec(select(ShopOrder).order_by(ShopOrder.created_at.desc()).limit(limit)).all()
    users = {u.id: u for u in session.exec(select(ShopUser)).all()}
    return [
        ShopOrderRead(
            **order.model_dump(),
            telegram_id=users[order.shop_user_id].telegram_id if order.shop_user_id in users else None,
            display_name=users[order.shop_user_id].display_name if order.shop_user_id in users else None,
        )
        for order in orders
    ]


# ══════════════════════════════════════════════════════ shop-bot endpoints


@bot_router.post("/session", response_model=ShopBotSession)
def bot_session(body: ShopBotSessionRequest, session: Session = Depends(get_session)):
    """Everything the bot needs to draw its menu for one customer, in one call.

    Returned together rather than as four endpoints because the bot renders
    them on the same screen: splitting them would make a menu tap depend on
    four round-trips any of which could be the one that fails.
    """
    user = get_or_create_shop_user(
        session,
        body.telegram_id,
        telegram_username=body.telegram_username,
        display_name=body.display_name,
    )
    settings = get_shop_settings(session)
    return ShopBotSession(
        shop_user_id=user.id,
        is_blocked=user.is_blocked,
        balance=wallet_balance(session, user.id),
        is_open=settings.is_open,
        price_per_gb=settings.price_per_gb,
        min_gb=settings.min_gb,
        max_gb=settings.max_gb,
        plan_duration_days=settings.plan_duration_days,
        card_number=settings.card_number,
        card_holder=settings.card_holder,
        min_topup=settings.min_topup,
        max_topup=settings.max_topup,
        shop_name=settings.shop_name,
        support_handle=settings.support_handle,
        approval_eta_minutes=settings.approval_eta_minutes,
        trial_enabled=settings.trial_enabled,
        trial_available=(settings.trial_enabled and user.trial_taken_at is None and not user.is_blocked
                         and not is_existing_customer(session, user.id)),
        trial_gb=settings.trial_gb,
        trial_hours=settings.trial_hours,
    )


@bot_router.post("/quote")
def bot_quote(body: ShopBotPurchaseRequest, session: Session = Depends(get_session)):
    """Price without buying. Lets the bot show a confirmation screen carrying
    the real number, rather than one the bot computed itself from a cached
    rate that may since have changed.

    Runs the SAME validation as /purchase. Quoting without it meant the
    confirm screen would happily price a 5000 GB plan against a 200 GB
    maximum, or quote at all while the shop was closed — the customer only
    discovering it after tapping buy. A quote that cannot be honoured is
    worse than no quote.
    """
    settings = get_shop_settings(session)
    try:
        validate_purchase_request(settings, body.data_limit_gb)
        return {"data_limit_gb": body.data_limit_gb, "price": quote_price(settings, body.data_limit_gb),
                "duration_days": settings.plan_duration_days}
    except ShopError as exc:
        raise HTTPException(400, str(exc))


@bot_router.post("/purchase", response_model=ShopPurchaseResult)
async def bot_purchase(body: ShopBotPurchaseRequest, session: Session = Depends(get_session)):
    user = session.exec(select(ShopUser).where(ShopUser.telegram_id == body.telegram_id)).first()
    if user is None:
        raise HTTPException(404, "Unknown shop user — call /session first")

    try:
        order = await purchase(session, user, body.data_limit_gb)
    except ShopError as exc:
        # A 400 with the user-facing sentence, which the bot shows verbatim.
        raise HTTPException(400, str(exc))

    subscription_url = None
    if order.account_id is not None:
        account = session.get(Account, order.account_id)
        if account is not None:
            subscription_url = resolve_subscription_url(account.subscription_url)

    if order.status == ShopOrderStatus.failed:
        # purchase() has already refunded. Told plainly, with the balance, so
        # the customer can see their money is back rather than assuming it is
        # gone and opening a support conversation.
        balance = wallet_balance(session, user.id)
        raise HTTPException(
            502,
            f"Couldn't create the account, so nothing was charged — your balance is {balance:,} T. "
            f"Please try again in a minute.",
        )

    # The same push every other purchase path ends with. Without it this
    # endpoint charges a wallet and the customer hears nothing back.
    await deliver_order_to_customer(session, order)

    return ShopPurchaseResult(
        order_id=order.id,
        marzban_username=order.marzban_username,
        data_limit_gb=order.data_limit_gb,
        duration_days=order.duration_days,
        price=order.price,
        subscription_url=subscription_url,
        balance=wallet_balance(session, user.id),
        status=order.status,
    )


def _own_order_or_404(session: Session, order_id: int, telegram_id: int) -> ShopOrder:
    """The order, but only if it belongs to the customer asking for it.

    Same 404 either way: whether order #900 exists is not something a caller
    should be able to learn by asking for someone else's.
    """
    order = session.get(ShopOrder, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    owner = session.exec(select(ShopUser).where(ShopUser.telegram_id == telegram_id)).first()
    if owner is None or order.shop_user_id != owner.id:
        raise HTTPException(404, "Order not found")
    return order


@bot_router.post("/purchase/{order_id}/deliver")
async def bot_deliver_qr(order_id: int, body: ShopBotOrderAction, session: Session = Depends(get_session)):
    """Sends (or re-sends) an order's QR to the buyer.

    Separate from /purchase so a delivery that fails — Telegram hiccup, the
    customer having blocked the bot — can be retried without going anywhere
    near the money path. Re-delivering is always safe: it creates nothing and
    charges nothing.
    """
    order = _own_order_or_404(session, order_id, body.telegram_id)
    user = session.get(ShopUser, order.shop_user_id)
    account = session.get(Account, order.account_id) if order.account_id else None
    if user is None or account is None:
        raise HTTPException(400, "This order has no delivered account to send")

    subscription_url = resolve_subscription_url(account.subscription_url)
    if not subscription_url:
        raise HTTPException(400, "Marzban returned no subscription link for this account")

    plan_line = format_plan_line(order.data_limit_gb, order.duration_days)
    caption = build_caption(account.marzban_username, subscription_url, plan_line)
    try:
        await send_photo_to_shop_user(
            user.telegram_id, subscription_qr_png(subscription_url), caption,
            filename=f"{account.marzban_username}.png",
        )
    except Exception as exc:  # noqa: BLE001 — the caller needs the real reason to decide whether to retry
        logger.exception("Order #%s: could not deliver the QR", order_id)
        raise HTTPException(502, f"Could not send the QR: {exc}")
    return {"delivered": True}


@bot_router.post("/orders", response_model=ShopOrderIntent)
def bot_create_order(body: ShopBotPurchaseRequest, session: Session = Depends(get_session)):
    """Records what the customer chose, BEFORE asking them for money.

    This is the order-first flow. The old shape made a first-time buyer fund a
    wallet before they could pick anything, which meant: inventing an amount
    with no idea what things cost, doing the price multiplication themselves,
    and — worst — coming BACK after approval to place the order they thought
    they had already placed. Most didn't. The money sat in a wallet and the
    subscription was never collected.

    Takes no money. Returns what the bot needs to decide which of two screens
    to show: a one-tap confirm when the wallet already covers it, or a payment
    request carrying a single exact figure when it doesn't.
    """
    user = session.exec(select(ShopUser).where(ShopUser.telegram_id == body.telegram_id)).first()
    if user is None:
        raise HTTPException(404, "Unknown shop user — call /session first")
    try:
        order = create_awaiting_order(session, user, body.data_limit_gb)
    except ShopError as exc:
        raise HTTPException(400, str(exc))

    balance = wallet_balance(session, user.id)
    settings = get_shop_settings(session)
    return ShopOrderIntent(
        order_id=order.id,
        data_limit_gb=order.data_limit_gb,
        duration_days=order.duration_days,
        price=order.price,
        balance=balance,
        # The amount still to transfer. Never negative — a customer whose
        # wallet more than covers the plan is shown a confirm button, not a
        # bill for a negative sum.
        shortfall=max(0, order.price - balance),
        payable_from_wallet=balance >= order.price,
        card_number=settings.card_number,
        card_holder=settings.card_holder,
        approval_eta_minutes=settings.approval_eta_minutes,
    )


@bot_router.post("/orders/{order_id}/pay", response_model=ShopPurchaseResult)
async def bot_pay_order(order_id: int, body: ShopBotOrderAction, session: Session = Depends(get_session)):
    """Pays an awaiting order from the wallet and delivers it.

    The repeat-customer path: someone with credit already in the wallet taps
    confirm and has their subscription seconds later, with no card transfer
    and no human in the loop. This is what the wallet is actually FOR — and it
    only earns its place once the customer has been through the flow once.
    """
    order = _own_order_or_404(session, order_id, body.telegram_id)
    try:
        order = await pay_awaiting_order(session, order)
    except ShopError as exc:
        raise HTTPException(400, str(exc))

    if order.status == ShopOrderStatus.failed:
        raise HTTPException(502, order.error or "Could not create the account")

    # The bot deliberately sends nothing but "working on it" and relies on
    # this push — the same delivery an approved card payment gets. Omitting it
    # here meant a wallet purchase charged the customer and then went silent.
    await deliver_order_to_customer(session, order)

    account = session.get(Account, order.account_id) if order.account_id else None
    return ShopPurchaseResult(
        order_id=order.id,
        marzban_username=order.marzban_username,
        data_limit_gb=order.data_limit_gb,
        duration_days=order.duration_days,
        price=order.price,
        balance=wallet_balance(session, order.shop_user_id),
        subscription_url=resolve_subscription_url(account.subscription_url) if account else None,
        status=order.status,
    )


@bot_router.post("/trial", response_model=ShopPurchaseResult)
async def bot_grant_trial(body: ShopBotSessionRequest, session: Session = Depends(get_session)):
    """Hands a first-time visitor a real, working subscription for nothing.

    The one endpoint that gives away inventory, and the reason it exists is in
    grant_trial's docstring: in a market with no escrow, no refunds and no
    ratings, a trial is the only mechanism by which the shop can go first.

    Delivery is done here rather than left to the bot so a trial arrives
    looking exactly like a purchase — same QR, same link, same setup guide.
    The customer's first experience of the product should be the real one.
    """
    user = get_or_create_shop_user(
        session,
        body.telegram_id,
        telegram_username=body.telegram_username,
        display_name=body.display_name,
    )
    try:
        order = await grant_trial(session, user)
    except ShopError as exc:
        raise HTTPException(400, str(exc))

    if order.status == ShopOrderStatus.failed:
        # The trial was already marked as taken before provisioning (see
        # grant_trial) so a retry loop can't mint free accounts. That means
        # this customer has lost their trial to a panel failure, which is the
        # operator's problem to fix, not something to paper over silently.
        logger.error("Trial order #%s failed for telegram_id=%s", order.id, body.telegram_id)
        raise HTTPException(502, order.error or "Could not create the trial account")

    await deliver_order_to_customer(session, order)

    account = session.get(Account, order.account_id) if order.account_id else None
    return ShopPurchaseResult(
        order_id=order.id,
        marzban_username=order.marzban_username,
        data_limit_gb=order.data_limit_gb,
        duration_days=order.duration_days,
        price=0,
        balance=wallet_balance(session, user.id),
        subscription_url=resolve_subscription_url(account.subscription_url) if account else None,
        status=order.status,
    )


@bot_router.get("/orders/pending", response_model=Optional[ShopOrderIntent])
def bot_pending_order(telegram_id: int, session: Session = Depends(get_session)):
    """The plan a receipt belongs to when the bot has lost the thread.

    Returns null when there is nothing waiting. See
    shop_service.latest_awaiting_order for why this is needed at all — mostly,
    a customer who turned their VPN off to open a banking app.
    """
    user = session.exec(select(ShopUser).where(ShopUser.telegram_id == telegram_id)).first()
    if user is None:
        return None
    order = latest_awaiting_order(session, user.id)
    if order is None:
        return None
    balance = wallet_balance(session, user.id)
    settings = get_shop_settings(session)
    return ShopOrderIntent(
        order_id=order.id,
        data_limit_gb=order.data_limit_gb,
        duration_days=order.duration_days,
        price=order.price,
        balance=balance,
        shortfall=max(0, order.price - balance),
        payable_from_wallet=balance >= order.price,
        card_number=settings.card_number,
        card_holder=settings.card_holder,
        approval_eta_minutes=settings.approval_eta_minutes,
    )


@bot_router.get("/topups/status")
def bot_topup_status(telegram_id: int, code: str, session: Session = Depends(get_session)):
    """What happened to the payment with this code — answered by the bot
    itself, instead of by a person the customer has to message and wait for.

    The reference code was given so the customer would hold something. This
    is what makes holding it useful: typing it back into the chat returns the
    answer immediately, at any hour.
    """
    user = session.exec(select(ShopUser).where(ShopUser.telegram_id == telegram_id)).first()
    if user is None:
        raise HTTPException(404, "Unknown shop user")
    topup = find_topup_by_code(session, user.id, code.strip().upper())
    if topup is None:
        raise HTTPException(404, "No payment with that code for this customer")
    order_status = None
    data_limit_gb = None
    if topup.order_id is not None:
        order = session.get(ShopOrder, topup.order_id)
        if order is not None:
            order_status = order.status.value
            data_limit_gb = order.data_limit_gb
    return {
        "reference_code": topup.reference_code,
        "status": topup.status.value,
        "claimed_amount": topup.claimed_amount,
        "approved_amount": topup.approved_amount,
        "reject_reason": topup.reject_reason,
        "order_status": order_status,
        "data_limit_gb": data_limit_gb,
    }


@bot_router.post("/topups", response_model=ShopTopupRead)
async def bot_create_topup(
    body: ShopBotTopupRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    user = session.exec(select(ShopUser).where(ShopUser.telegram_id == body.telegram_id)).first()
    if user is None:
        raise HTTPException(404, "Unknown shop user — call /session first")
    try:
        topup = create_topup(session, user, body.claimed_amount, body.receipt_file_id,
                             order_id=body.order_id)
    except ShopError as exc:
        raise HTTPException(400, str(exc))

    # Backgrounded: the customer's confirmation should not wait on, or fail
    # because of, a Telegram call to the OPERATOR. The row is already
    # committed, so a failed alert loses the notification, not the request —
    # and the dashboard's pending list shows it regardless.
    order_summary = None
    if topup.order_id is not None:
        bound = session.get(ShopOrder, topup.order_id)
        if bound is not None:
            order_summary = f"{bound.data_limit_gb:g} GB / {bound.duration_days} days, price {bound.price:,} T"
    background_tasks.add_task(_alert_operator_to_topup, topup.id, user.telegram_id,
                              user.display_name or user.telegram_username, body.receipt_file_id,
                              topup.claimed_amount, topup.reference_code, order_summary)

    return ShopTopupRead(**topup.model_dump(), telegram_id=user.telegram_id, display_name=user.display_name)


async def _alert_operator_to_topup(
    topup_id: int,
    telegram_id: int,
    who: Optional[str],
    receipt_file_id: Optional[str],
    claimed_amount: int,
    reference_code: Optional[str] = None,
    order_summary: Optional[str] = None,
) -> None:
    # The reference code is shown because the customer was given it and will
    # quote it back. The order line matters more: approving an order-bound
    # payment also DELIVERS a plan, and approving less than it costs leaves the
    # customer waiting for one — the operator needs to know which kind of
    # approval they are making before they tap.
    ref = f" · code {reference_code}" if reference_code else ""
    kind = (f"\nFor: {order_summary} (approving delivers it)" if order_summary
            else "\nFor: wallet credit only")
    caption = (
        f"💳 New payment #{topup_id}{ref}\n"
        f"From: {who or 'unknown'} (id {telegram_id})\n"
        f"Claimed: {claimed_amount:,} T"
        f"{kind}"
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": f"✅ Approve {claimed_amount:,} T", "callback_data": f"topup:ok:{topup_id}"},
            {"text": "❌ Reject", "callback_data": f"topup:no:{topup_id}"},
        ]]
    }
    try:
        if receipt_file_id:
            # Preferred: the operator sees the receipt itself next to the
            # buttons, which is the whole decision they're being asked to make.
            await relay_shop_photo_to_admin(receipt_file_id, caption, keyboard)
        else:
            await notify_admin_with_buttons(caption, keyboard)
    except Exception:
        logger.exception("Could not send the receipt image for top-up #%s", topup_id)
        # The image is the nice-to-have; the BUTTONS are what let the operator
        # decide from Telegram at all, so losing the image must not cost them.
        try:
            await notify_admin_with_buttons(
                caption + "\n\n(The receipt image could not be attached - see the dashboard.)",
                keyboard,
            )
        except Exception:
            logger.exception("Could not alert the operator to top-up #%s with buttons", topup_id)
            try:
                await notify_admin(caption + "\n\n(Open the dashboard's Shop page to approve.)")
            except Exception:
                logger.exception("Could not alert the operator to top-up #%s at all", topup_id)


@bot_router.get("/accounts", response_model=list[ShopBotAccountRow])
def bot_list_accounts(telegram_id: int, session: Session = Depends(get_session)):
    """The customer's own delivered accounts, with live-ish usage from the
    last sync. Scoped by their orders — never by a name pattern, which would
    hand someone else's account to anyone who guessed a username."""
    user = session.exec(select(ShopUser).where(ShopUser.telegram_id == telegram_id)).first()
    if user is None:
        return []
    orders = session.exec(
        select(ShopOrder)
        .where(ShopOrder.shop_user_id == user.id, ShopOrder.status == ShopOrderStatus.delivered)
        .order_by(ShopOrder.created_at.desc())
    ).all()

    rows: list[ShopBotAccountRow] = []
    for order in orders:
        account = session.get(Account, order.account_id) if order.account_id else None
        rows.append(ShopBotAccountRow(
            order_id=order.id,
            marzban_username=order.marzban_username or "—",
            data_limit_gb=order.data_limit_gb,
            used_traffic=account.used_traffic if account else 0,
            data_limit=account.data_limit if account else None,
            expire=account.expire if account else None,
            status=account.status if account else None,
            subscription_url=resolve_subscription_url(account.subscription_url) if account else None,
            created_at=order.created_at,
        ))
    return rows


@bot_router.get("/wallet", response_model=list[ShopWalletEntryRead])
def bot_wallet_history(telegram_id: int, limit: int = 20, session: Session = Depends(get_session)):
    user = session.exec(select(ShopUser).where(ShopUser.telegram_id == telegram_id)).first()
    if user is None:
        return []
    return session.exec(
        select(ShopWalletEntry)
        .where(ShopWalletEntry.shop_user_id == user.id)
        .order_by(ShopWalletEntry.created_at.desc())
        .limit(limit)
    ).all()
