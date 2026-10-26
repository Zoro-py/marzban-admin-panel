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

import json
import re

import logging
import secrets
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from sqlmodel import Session, select

from app.auth import require_auth
from app.bulk_accounts import build_caption, format_plan_line, resolve_subscription_url
from app.config import settings as app_settings
from app.db import engine, get_session
from app import shop_texts
from app.models import (
    Account,
    BillingMode,
    Customer,
    Group,
    ShopOrder,
    ShopOrderStatus,
    ShopBindClaim,
    ShopTopup,
    ShopTopupStatus,
    ShopUser,
    ShopWalletEntry,
    ShopWalletEntryType,
    utcnow,
    AccountEvent,
)
from app.notify import (
    relay_shop_photo_to_admin,
    notify_admin,
    notify_admin_with_buttons,
    send_photo_to_shop_user,
    send_to_shop_user,
)
from app.qr import subscription_qr_png
from app.marzban_client import marzban_client
from app.schemas import (
    ShopBotAccountRow,
    ShopBotClaimLinkRequest,
    ShopBotClaimLinkResult,
    ShopBotOrderAction,
    ShopBotPhoneRequest,
    ShopBotPurchaseRequest,
    ShopBotSession,
    ShopBotPayDebtRequest,
    ShopBotPayDebtResult,
    ShopBotSessionRequest,
    ShopBotTopupRequest,
    ShopBindClaimRead,
    ShopBindClaimRequest,
    ShopLinkInviteRead,
    ShopLinkInviteRequest,
    ShopLinkState,
    ShopBotServiceNameRequest,
    ShopTelegramBindRequest,
    ShopTelegramBindResult,
    ShopAutoRenewResult,
    ShopOrderIntent,
    ShopOrderRead,
    ShopPaygStartRequest,
    ShopPaygStartResult,
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
    DEBT_SUSPEND_AFTER_DAYS,
    ShopConflict,
    ShopError,
    approve_topup,
    auto_renew_account,
    create_awaiting_order,
    create_topup,
    deliver_order_to_customer,
    find_prior_receipt_text_use,
    find_topup_by_code,
    get_or_create_shop_user,
    STUCK_ORDER_TIMEOUT_MINUTES,
    get_shop_settings,
    grant_trial,
    is_existing_customer,
    latest_awaiting_order,
    maybe_grant_provisional,
    pay_awaiting_order,
    post_wallet_entry,
    purchase,
    quote_price,
    reactivate_suspended_payg_accounts,
    renewable_account,
    refuse_unlimited_renewal,
    reject_topup,
    revoke_provisional,
    start_payg_service,
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
async def read_settings(session: Session = Depends(get_session)):
    row = get_shop_settings(session)
    if row.shop_inbound_tags:
        try:
            row.shop_inbound_tags = json.loads(row.shop_inbound_tags)
        except (ValueError, TypeError):
            row.shop_inbound_tags = None
    return row


@router.patch("/settings", response_model=ShopSettingsRead)
async def update_settings(body: ShopSettingsUpdate, session: Session = Depends(get_session)):
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

    merged_payg_on = updates.get("payg_enabled", settings.payg_enabled)
    merged_payg_rate = updates.get("payg_rate_per_gb", settings.payg_rate_per_gb)
    if merged_payg_on and not (merged_payg_rate and merged_payg_rate > 0):
        raise HTTPException(400, "Set a payg rate per GB before enabling pay-as-you-go")

    # F4: zeroing the RATE is refused while any live retail-payg account
    # exists — REGARDLESS of payg_enabled in this same request, because the
    # daily meter (shop_service._sweep_payg_accounts) runs on billing_mode,
    # not on the offer switch: turning the offer off is legal, but those
    # accounts keep being metered, and with a None/0 rate their charge
    # computes to zero — the meter silently stops and nobody is told.
    # (payg_enabled=false alone, leaving the rate as-is, is NOT blocked here.)
    if not (merged_payg_rate and merged_payg_rate > 0):
        live_retail_payg = session.exec(
            select(Account.id).where(
                Account.billing_mode == BillingMode.payg,
                Account.customer_id.is_(None),
                Account.status != "deleted_from_marzban",
            )
        ).all()
        if live_retail_payg:
            raise HTTPException(
                400,
                f"payg_rate_per_gb cannot be cleared while {len(live_retail_payg)} live pay-as-you-go "
                "account(s) are still metered daily — they need a rate. Suspend or remove those "
                "accounts first, or keep a rate set.",
            )

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

    # Shop inbound selection: validate every tag against the server's LIVE
    # inbounds (a stale/typo'd tag would silently fall back to Marzban
    # defaults for THAT tag — better to refuse with the reason), then store
    # as JSON (the column is TEXT). Empty list = clear the selection.
    if "shop_inbound_tags" in updates:
        wanted = sorted(set(updates["shop_inbound_tags"] or []))
        if wanted:
            try:
                hosts = await marzban_client.list_inbounds()
            except Exception as exc:  # noqa: BLE001 — cannot validate → refuse, don't guess
                raise HTTPException(502, f"Could not validate the inbound selection against Marzban: {exc}")
            live_tags = {i.get("tag") for inbs in (hosts or {}).values() for i in inbs or []}
            unknown = [t for t in wanted if t not in live_tags]
            if unknown:
                raise HTTPException(
                    400,
                    "These inbound tags don't exist on the server anymore: " + ", ".join(unknown),
                )
        updates["shop_inbound_tags"] = json.dumps(wanted) if wanted else None

    for field, value in updates.items():
        setattr(settings, field, value)
    session.add(settings)
    session.commit()
    session.refresh(settings)
    if settings.shop_inbound_tags:
        try:
            settings.shop_inbound_tags = json.loads(settings.shop_inbound_tags)
        except (ValueError, TypeError):
            settings.shop_inbound_tags = None
    return settings


@router.get("/inbounds")
async def list_shop_inbounds(session: Session = Depends(get_session)):
    """The server's LIVE Marzban inbounds, grouped by tag, plus which ones the
    operator has currently selected for shop-created accounts. The UI renders
    this as the checkbox list on the Shop settings tab — one source of truth,
    so a tag that no longer exists on the server can never be re-selected."""
    settings_row = get_shop_settings(session)
    selected: list[str] = []
    if settings_row.shop_inbound_tags:
        try:
            selected = json.loads(settings_row.shop_inbound_tags)
        except (ValueError, TypeError):
            selected = []
    try:
        hosts = await marzban_client.list_inbounds()
    except Exception as exc:  # noqa: BLE001 — surface as 502, the UI shows a toast
        raise HTTPException(502, f"Could not read Marzban's inbound list: {exc}")
    grouped: dict[str, dict] = {}
    for host, inbs in (hosts or {}).items():
        for i in inbs or []:
            entry = grouped.setdefault(
                i.get("tag", ""), {"tag": i.get("tag"), "protocol": i.get("protocol"), "hosts": []}
            )
            if host not in entry["hosts"]:
                entry["hosts"].append(host)
    inbounds = sorted(grouped.values(), key=lambda e: (e["protocol"], e["tag"]))
    for e in inbounds:
        e["selected"] = e["tag"] in selected
    return {"inbounds": inbounds, "selected": [t for t in selected]}


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
def list_wallet_entries(user_id: int, limit: int = Query(100, ge=1, le=500), session: Session = Depends(get_session)):
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
    limit: int = Query(100, ge=1, le=500),
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

    # F7: this approval just funded the wallet, which is exactly the moment a
    # payg service the meter suspended comes back — "top up and you're back"
    # must not wait for tomorrow's daily meter. Strictly after the credit
    # committed (inside approve_topup), and best-effort: the money part of the
    # approval is done, so a reactivation failure must not fail the request —
    # the helper logs and tells the operator instead.
    if user is not None:
        try:
            await reactivate_suspended_payg_accounts(session)
        except Exception:
            logger.exception("Top-up #%s approved but payg reactivation failed", topup.id)

    # The reactivation pass commits (the account flip), and a commit expires
    # every instance in the session's identity map — including this topup.
    # model_dump() on an expired SQLModel table row returns {} (fields live in
    # __dict__ until reloaded), which used to 500 the approval AFTER the money
    # had credited. Re-select before serialising.
    session.refresh(topup)

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
        # A bridge service handed out for THIS payment stops with it —
        # otherwise a rejected receipt would still buy working service.
        stopped = False
        try:
            stopped = await revoke_provisional(session, topup.shop_user_id)
        except Exception:
            logger.exception("Could not stop the bridge service after rejecting top-up #%s", topup.id)
        # Through shop_texts so a rejection is never reason-less: from where the
        # customer sits, a bare "no" after sending money to a personal card is
        # indistinguishable from theft.
        try:
            message = shop_texts.topup_rejected(
                topup.reference_code, topup.reject_reason,
                get_shop_settings(session).support_handle,
            )
            if stopped:
                message += shop_texts.provisional_stopped()
            await send_to_shop_user(user.telegram_id, message)
        except Exception:
            logger.exception("Top-up #%s rejected but the customer could not be notified", topup.id)

    return ShopTopupRead(**topup.model_dump(), telegram_id=user.telegram_id if user else None,
                         display_name=user.display_name if user else None)


@router.get("/orders", response_model=list[ShopOrderRead])
def list_orders(limit: int = Query(100, ge=1, le=500), session: Session = Depends(get_session)):
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


# ══════════════════════════════════════ linking EXISTING customers to the shop
#
# The operator's ~163 hand-created customers have never touched the shop bot.
# A one-time invite link binds the CUSTOMER (models.Customer.shop_user_id) to
# the shop identity the person taps it from — after which their existing
# accounts show up in «سرویس‌های من», and a normal purchase renews their real
# account in place, paid from the shop wallet (see shop_service.renewable_account's
# linked-customer fallback). No money moves at link time in either direction:
# this binds identities, never balances. The delegate invite flow
# (routers/delegate.py) is the pattern deliberately mirrored here — one-time
# token, 7-day TTL, uniform 404 on unknown/expired/used.


# How long a shop-link deep link stays claimable — same value and reasoning
# as the delegate invite's INVITE_TTL_DAYS: long enough to batch-message every
# customer at leisure, short enough that a forwarded-to-the-wrong-chat link
# stops being a live claim inside a week.
SHOP_LINK_TTL_DAYS = 7

# The deep-link start payload prefix (t.me/<bot>?start=shoplnk_<token>).
# shopbot's start handler matches this same literal — a two-side constant
# with no shared import (the bot and the backend are separate processes by
# design), so changing one means changing both.
SHOP_LINK_PREFIX = "shoplnk_"


def _shop_invite_url(token: str) -> str:
    return (
        f"https://t.me/{app_settings.shop_bot_username.strip().lstrip('@')}"
        f"?start={SHOP_LINK_PREFIX}{token}"
    )


def _customer_has_payg_billing(session: Session, customer_id: int) -> bool:
    """Does any account this customer owns directly bill pay-as-you-go?

    The rule this enforces is the two-economies rule from models.py's shop
    section header: a payg account's monthly settle charges the reseller
    LEDGER from the usage meter, while a shop purchase would debit the
    wallet and extend the SAME account — the shop payment would never appear
    in the settle and the settle would never know the service was renewed.
    Group members aren't examined: an account owned by the customer directly
    (customer_id set) is what the link exposes and extends, and that is the
    set checked here. A customer with no accounts has nothing to shadow.
    """
    accounts = session.exec(select(Account).where(Account.customer_id == customer_id)).all()
    for account in accounts:
        if account.group_id is not None:
            group = session.get(Group, account.group_id)
            if group is not None and group.billing_mode == BillingMode.payg:
                return True
        elif account.billing_mode == BillingMode.payg:
            return True
    return False


def _payg_partition(session: Session, customer: Customer) -> tuple[list[Account], list[str]]:
    """The link-blocking payg surface of this customer, split by WHO can fix
    it (owner 2026-10-24: payg customers must be SUPPORTED, not refused):

      * standalone payg accounts (customer_id is this customer, no group) —
        convertible right here: settle the accrued usage, flip to prepay via
        the same guarded endpoint the panel's billing editor uses, then link.
      * group members whose GROUP bills payg — NOT convertible here: flipping
        the group reprices every member (a group-wide decision the operator
        makes in the group's own settings); returns the group NAMES so the
        refusal can say exactly what to go flip.

    The one source both the refusals and the one-click conversion read, so
    the rule can never drift between them."""
    standalone: list[Account] = []
    group_names: list[str] = []
    accounts = session.exec(select(Account).where(Account.customer_id == customer.id)).all()
    for account in accounts:
        if account.group_id is not None:
            group = session.get(Group, account.group_id)
            if group is not None and group.billing_mode == BillingMode.payg and group.name not in group_names:
                group_names.append(group.name)
        elif account.billing_mode == BillingMode.payg:
            standalone.append(account)
    return standalone, group_names


_PAYG_REFUSAL_TEXT = (
    "این مشتری بیلینگ مصرفی (payg) دارد: تسویهٔ ماهانه از لجر برای مصرف واقعی بیل "
    "می‌زند ولی خریدِ فروشگاه از کیف پول همان اکانت را تمدید می‌کند — دو نظامِ پولی "
    "روی یک شمارنده سایه می‌اندازند و مشتری دو بار پول می‌دهد. مسیر پشتیبانی‌شده: "
    "در پنل، دکمهٔ «تسویه + تبدیل + اتصال» (مصرفِ مانده بیل و ثبت می‌شود، اکانت‌ها "
    "به پکیچی تبدیل و بعد وصل می‌شوند)."
)


def _linked_customer(session: Session, shop_user: ShopUser) -> Optional[Customer]:
    """The customer this shop user is linked to — resolving BOTH link
    directions, because only one is authoritative in the live data: the
    bind/claim flows write Customer.shop_user_id (the live link), while
    ShopUser.customer_id is a reporting-only annotation some rows carry and
    most do not. Found-live (2026-10-24): reading only the annotation side
    made the debt feature silently inert for every real link."""
    if shop_user.customer_id is not None:
        customer = session.get(Customer, shop_user.customer_id)
        if customer is not None and customer.shop_user_id == shop_user.id:
            return customer
    return session.exec(
        select(Customer).where(Customer.shop_user_id == shop_user.id)
    ).first()


def _linked_ledger_debt(session: Session, shop_user: ShopUser) -> float:
    """What this shop user's linked CUSTOMER owes on the reseller ledger right
    now — MoneyBook's net_owed (posted + not-yet-invoiced), i.e. exactly the
    figure the panel shows as «Owes now». 0.0 for an unlinked shop user.

    READ-ONLY by contract (council 2026-24, routed books): the wallet's
    affordability and this debt meet only in DISPLAY and in the customer's
    explicit payment choices — never as a bookkeeping transfer."""
    from app.shop_service import linked_debt

    return round(linked_debt(session, shop_user.id), 2)


def _linked_account_count(session: Session, customer: Customer) -> int:
    """How many of the customer's accounts the shop will show once linked —
    the same set bot_list_accounts merges in: every directly-owned account
    except one deleted from Marzban (a disabled one still SHOWS as disabled;
    a deleted one shows nothing, matching Marzban itself)."""
    accounts = session.exec(select(Account).where(Account.customer_id == customer.id)).all()
    return sum(1 for a in accounts if a.status != "deleted_from_marzban")


@router.post("/link-invite", response_model=ShopLinkInviteRead)
def create_shop_link_invite(body: ShopLinkInviteRequest, session: Session = Depends(get_session)):
    """Mint a one-time t.me deep link that binds an existing customer to the
    shop bot. The customer taps it, the claim endpoint (POST
    /api/shop/bot/claim-link) sets Customer.shop_user_id, and their accounts
    become visible in the bot with no per-account rows to maintain.

    Every refusal below is a different sentence on purpose: the operator is
    about to send this link to a real person, and a bare "not allowed" would
    send them digging for which rule tripped. Deliberately refuses to stack
    pending invites for one customer, exactly like the delegate invite —
    every extra live token is one more unguessable claim on this customer
    that the operator has no overview of."""
    username = app_settings.shop_bot_username.strip().lstrip("@")
    if not username:
        raise HTTPException(
            503,
            "Shop bot username not configured (SHOP_BOT_USERNAME) — "
            "invite links would point nowhere",
        )
    customer = session.get(Customer, body.customer_id)
    if not customer:
        raise HTTPException(404, "Customer not found")
    if customer.is_group_rep:
        raise HTTPException(
            409,
            "This customer is a group/family representative — exposing a shared "
            "group's accounts to one personal Telegram account would let one "
            "person see and renew everyone's service. Delegate access covers "
            "that case instead.",
        )
    if _customer_has_payg_billing(session, customer.id):
        raise HTTPException(409, _PAYG_REFUSAL_TEXT)
    if customer.shop_user_id is not None:
        shop_user = session.get(ShopUser, customer.shop_user_id)
        bound = f"telegram id {shop_user.telegram_id}" if shop_user else "an unknown shop user"
        raise HTTPException(
            409,
            f"This customer is already linked to the shop bot ({bound}) — "
            f"unlink first: DELETE /api/shop/link/{customer.id}",
        )
    if customer.shop_link_token is not None:
        raise HTTPException(
            409,
            "A pending shop-link invite already exists for this customer "
            f"(expires {customer.shop_link_expires_at}) — reuse it, or discard "
            f"it first: DELETE /api/shop/link-invite/{customer.id}",
        )

    customer.shop_link_token = secrets.token_urlsafe(16)
    customer.shop_link_expires_at = utcnow() + timedelta(days=SHOP_LINK_TTL_DAYS)
    session.add(customer)
    session.commit()
    session.refresh(customer)
    return ShopLinkInviteRead(
        customer_id=customer.id,
        customer_name=customer.name,
        invite_url=_shop_invite_url(customer.shop_link_token),
        claim_expires_at=customer.shop_link_expires_at,
    )


@router.get("/link/{customer_id}", response_model=ShopLinkState)
def read_shop_link(customer_id: int, session: Session = Depends(get_session)):
    """The one endpoint the panel's Shop-bot card renders from: which of the
    three states the customer is in, plus whatever that state displays."""
    customer = session.get(Customer, customer_id)
    if not customer:
        raise HTTPException(404, "Customer not found")
    claim = session.exec(
        select(ShopBindClaim).where(ShopBindClaim.customer_id == customer_id, ShopBindClaim.claimed_at.is_(None))
    ).first()

    def _claim_kwargs() -> dict:
        return {
            "bind_claim_username": claim.telegram_username if claim else None,
            "bind_claim_display_name": claim.display_name if claim else None,
            "bind_claim_created_at": claim.created_at if claim else None,
            **_arrival_kwargs(),
        }

    def _arrival_kwargs() -> dict:
        # The person who actually wrote to the bot under the claimed identity
        # (payg claims wait here for the operator's confirm - see
        # _try_complete_bind_claim).
        if claim is None or claim.claimed_telegram_id is None:
            return {"bind_claim_arrived_telegram_id": None, "bind_claim_arrived_name": None, "bind_claim_arrived_username": None}
        arrived = session.exec(select(ShopUser).where(ShopUser.telegram_id == claim.claimed_telegram_id)).first()
        return {
            "bind_claim_arrived_telegram_id": claim.claimed_telegram_id,
            "bind_claim_arrived_name": arrived.display_name if arrived else None,
            "bind_claim_arrived_username": arrived.telegram_username if arrived else None,
        }

    def _payg_kwargs() -> dict:
        # The one-click preview: what the conversion would bill (standalone
        # accounts' pending, one MoneyBook snapshot) and whether any of the
        # payg comes from groups (not convertible here). For a LINKED
        # customer this is always empty/false — the flip guard keeps linked
        # customers prepay — but computing it anyway keeps the payload honest
        # if that invariant is ever violated.
        from app.services import MoneyBook

        standalone, group_names = _payg_partition(session, customer)
        book = MoneyBook(session)
        # The customer's WHOLE debt (posted + not-yet-invoiced) - the figure the
        # panel's «Owes now» shows - next to what the conversion bills this
        # click. Showing only the latter made a 949k debt read as 143k
        # (owner report 2026-10-26).
        owed_now = round(book.customer_net(customer), 2)
        if not standalone and not group_names:
            return {"has_payg": False, "payg_pending_amount": 0.0, "payg_group_names": [], "owed_now": owed_now}
        pending = round(sum(book.account_pending(a) for a in standalone), 2)
        return {
            "has_payg": True,
            "payg_pending_amount": pending,
            "payg_group_names": group_names,
            "owed_now": owed_now,
        }

    if customer.shop_user_id is not None:
        shop_user = session.get(ShopUser, customer.shop_user_id)
        return ShopLinkState(
            customer_id=customer.id,
            customer_name=customer.name,
            status="linked",
            linked_telegram_id=shop_user.telegram_id if shop_user else None,
            linked_telegram_username=shop_user.telegram_username if shop_user else None,
            linked_display_name=shop_user.display_name if shop_user else None,
            # Other customers riding on the same Telegram (family bought by one person).
            also_linked_customers=[
                c.name for c in session.exec(
                    select(Customer).where(Customer.shop_user_id == customer.shop_user_id, Customer.id != customer.id)
                    .order_by(Customer.id)
                ).all()
            ],
            linked_at=customer.shop_link_linked_at,
            accounts_linked=_linked_account_count(session, customer),
            **_claim_kwargs(),
            **_payg_kwargs(),
        )
    if customer.shop_link_token is not None:
        username = app_settings.shop_bot_username.strip().lstrip("@")
        return ShopLinkState(
            customer_id=customer.id,
            customer_name=customer.name,
            status="pending",
            # Rebuilt, not stored: the token IS the stored state, the URL is
            # derived — so a SHOP_BOT_USERNAME fix repairs old pending links
            # for free.
            invite_url=_shop_invite_url(customer.shop_link_token) if username else None,
            claim_expires_at=customer.shop_link_expires_at,
            **_claim_kwargs(),
            **_payg_kwargs(),
        )
    return ShopLinkState(
        customer_id=customer.id,
        customer_name=customer.name,
        status="not_linked",
        **_claim_kwargs(),
        **_payg_kwargs(),
    )


@router.post("/bind-claim", response_model=ShopBindClaimRead)
def create_bind_claim(body: ShopBindClaimRequest, session: Session = Depends(get_session)):
    """Bind a customer by the identity the operator KNOWS — the owner's
    2026-10-24 ask: typing a numeric Telegram id requires ASKING the customer
    for a number nobody remembers. The operator types the @username (and/or
    the exact display name as Telegram shows it); the claim sits pending and
    the customer's FIRST message to the shop bot completes the bind — the bot
    itself reports the numeric id (see bot_session's _try_complete_bind_claim
    hook). Same bind guards as the direct bind run HERE, so an impossible
    claim is refused at typing time, not discovered missing later."""
    customer = session.get(Customer, body.customer_id)
    if not customer:
        raise HTTPException(404, "Customer not found")
    username = _normalize_claim_username(body.telegram_username)
    display = _normalize_claim_display(body.display_name)
    if username is None and display is None:
        raise HTTPException(400, "حداقل یکی از @username یا نام نمایشی لازم است")
    refusal = _bind_guards(customer)
    if refusal:
        raise HTTPException(409, refusal)
    _standalone_payg, payg_group_names = _payg_partition(session, customer)
    if payg_group_names:
        # A group flip reprices every member - that decision lives in the
        # group's own settings, never inside a bind (same rule as the direct bind).
        raise HTTPException(
            409,
            f"بیلینگِ مصرفیِ این مشتری از گروه می‌آید ({'، '.join(payg_group_names)}) — "
            "حالتِ گروه را جداگانه prepay کن (صفحهٔ گروه ← تنظیمات)، بعد دوباره امتحان کن.",
        )
    # A standalone-payg customer MAY hold a claim: the claim never binds and
    # never converts. When the person writes to the bot, only their numeric id
    # is RECORDED (_try_complete_bind_claim); the operator then confirms the
    # settle+convert+link in the panel, watching the amount (owner rule: money
    # steps are never triggered by a bot message). Council 2026-10-25.
    if customer.shop_user_id is not None:
        raise HTTPException(409, "این مشتری از قبل به تلگرام وصل است — اول جدا کنید (unlink)، بعد ادعای جدید بسازید.")
    # One PENDING claim per customer — a corrected retry replaces the old
    # one without needing a discard first.
    for old_claim in session.exec(
        select(ShopBindClaim).where(ShopBindClaim.customer_id == customer.id, ShopBindClaim.claimed_at.is_(None))
    ).all():
        session.delete(old_claim)
    claim = ShopBindClaim(
        customer_id=customer.id,
        telegram_username=username,
        display_name=display,
    )
    session.add(claim)
    session.commit()
    session.refresh(claim)
    return claim


@router.delete("/bind-claim/{customer_id}", response_model=ShopBindClaimRead)
def discard_bind_claim(customer_id: int, session: Session = Depends(get_session)):
    """Discard the customer's pending bind-by-identity claim (a typo, or the
    operator learned the numeric id and used the direct bind instead)."""
    claim = session.exec(
        select(ShopBindClaim).where(ShopBindClaim.customer_id == customer_id, ShopBindClaim.claimed_at.is_(None))
    ).first()
    if not claim:
        raise HTTPException(404, "No pending bind claim for this customer")
    session.delete(claim)
    session.commit()
    return claim


@router.delete("/link-invite/{customer_id}")
def discard_shop_link_invite(customer_id: int, session: Session = Depends(get_session)):
    """Discards a still-pending invite — the token columns are cleared and
    the deep link dies. A CLAIMED link is refused here: it isn't pending
    anymore, and the operator's off-switch for a live link is the unlink
    endpoint below, not a delete that would imply nothing was ever granted."""
    customer = session.get(Customer, customer_id)
    if not customer:
        raise HTTPException(404, "Customer not found")
    if customer.shop_user_id is not None:
        raise HTTPException(
            409,
            "This customer's shop link was already claimed — unlink it with "
            f"DELETE /api/shop/link/{customer_id} instead of discarding it",
        )
    if customer.shop_link_token is None:
        raise HTTPException(404, "No pending shop-link invite for this customer")
    customer.shop_link_token = None
    customer.shop_link_expires_at = None
    session.add(customer)
    session.commit()
    return {"ok": True}


@router.delete("/link/{customer_id}")
def unlink_shop_customer(customer_id: int, session: Session = Depends(get_session)):
    """The operator's off-switch. Clears the customer-side link (and any
    pending invite with it); the ShopUser, its wallet, and every account row
    are untouched — the customer just loses bot access and their accounts
    stop appearing in «سرویس‌های من». No money moves in either direction."""
    customer = session.get(Customer, customer_id)
    if not customer:
        raise HTTPException(404, "Customer not found")
    if customer.shop_user_id is None and customer.shop_link_token is None:
        raise HTTPException(404, "This customer is not linked and has no pending invite")
    customer.shop_user_id = None
    customer.shop_link_token = None
    customer.shop_link_expires_at = None
    customer.shop_link_linked_at = None
    session.add(customer)
    session.commit()
    return {"ok": True}


@router.post("/link/{customer_id}/telegram", response_model=ShopTelegramBindResult)
async def bind_shop_customer_telegram(
    customer_id: int,
    body: ShopTelegramBindRequest,
    session: Session = Depends(get_session),
    # Injected for the convert_payg path: the settle/flip calls below write
    # ledger rows and AccountEvents that must carry WHO decided the conversion.
    operator: str = Depends(require_auth),
):
    """The operator types the customer's Telegram id straight into the panel —
    the direct counterpart of the invite-link flow, for when the operator
    already KNOWS who the customer is on Telegram and doesn't need a
    consent round-trip through a deep link. The bot then knows which
    accounts belong to which Telegram user exactly as if the customer had
    claimed an invite: same writes (Customer.shop_user_id), same refusals,
    same trust consequences — this hands «سرویس‌های من», the wallet and
    renewals for THIS customer's accounts to that Telegram account, so every
    refusal below is a different sentence on purpose, and a wrong id is
    recoverable the same way as a mis-sent invite: unlink, rebind.

    NOT the same as the claim in one respect: there is no token, so there is
    nothing to consume — a still-pending invite is simply superseded (its
    token dies here; a claim arriving later finds it gone and 404s).

    notification_delivered=False is NOT a failure: Telegram refuses
    bot-initiated contact with someone who has never pressed /start, so the
    confirmation DM lands only for customers who already met the bot. The
    binding itself works either way."""
    customer = session.get(Customer, customer_id)
    if not customer:
        raise HTTPException(404, "Customer not found")
    if customer.is_group_rep:
        raise HTTPException(
            409,
            "این مشتری نمایندهٔ گروه/خانواده است — اتصالِ سرویس‌های مشترکِ گروه به یک "
            "تلگرامِ شخصی یعنی یک نفر همه را ببیند و تمدید کند. برای این حالت، دسترسیِ "
            "دلیگیت جایگزین است.",
        )
    # Every refusal that does NOT depend on the conversion runs BEFORE it:
    # the settle+flip below writes ledger rows, resets the Marzban meter and
    # flips billing modes, none of which a later 409 can undo. Found by
    # tests/test_shop_payg_link_edges.py E1/E2 (2026-10-25): an id already
    # bound to another customer used to bill 100,000 T and flip the account to
    # prepay, THEN answer 409 — money moved, nothing linked.
    existing_shop_user = session.exec(
        select(ShopUser).where(ShopUser.telegram_id == body.telegram_id)
    ).first()
    if customer.shop_user_id is not None and (
        existing_shop_user is None or customer.shop_user_id != existing_shop_user.id
    ):
        other = session.get(ShopUser, customer.shop_user_id)
        bound = f"telegram id {other.telegram_id}" if other else "an unknown shop user"
        raise HTTPException(
            409,
            f"این مشتری از قبل به تلگرامِ دیگری وصل است ({bound}) — اول با "
            f"DELETE /api/shop/link/{customer.id} جدا کنید، بعد دوباره ببندید.",
        )
    converted_payg_accounts = 0
    settled_amount = 0.0
    payg_standalone, payg_groups = _payg_partition(session, customer)
    if payg_standalone or payg_groups:
        if payg_groups:
            # A group flip reprices EVERY member — that decision belongs to
            # the group's own settings screen, not inside a bind call.
            raise HTTPException(
                409,
                f"بیلینگِ مصرفیِ این مشتری از گروه می‌آید ({'، '.join(payg_groups)}) — "
                "حالتِ گروه را جداگانه prepay کن (صفحهٔ گروه ← تنظیمات؛ این تصمیم همهٔ "
                "اعضا را عوض می‌کند)، بعد دوباره وصل کن.",
            )
        if not body.convert_payg:
            raise HTTPException(409, _PAYG_REFUSAL_TEXT)
        # The supported path (owner 2026-10-24): settle each standalone payg
        # account (the charge for accrued usage becomes real — visible debt,
        # never forgiven), flip it to prepay through the SAME guarded endpoint
        # the panel's billing editor uses (the D21 billed-baseline
        # neutralization rides along), then fall through to the ordinary
        # bind. settle_account / update_billing are called DIRECTLY — zero
        # money logic duplicated here. Each takes the billing lock internally,
        # so this endpoint must never hold it (the lock is not reentrant).
        from app.routers.accounts import settle_account as _settle_account
        from app.routers.accounts import update_billing as _update_billing
        from app.schemas import AccountSettleRequest, AccountBillingUpdate

        for account in payg_standalone:
            try:
                result = await _settle_account(
                    account.id, AccountSettleRequest(), session, operator
                )
                settled_amount += float(result.get("charged_amount") or 0.0)
                await _update_billing(
                    account.id,
                    AccountBillingUpdate(billing_mode=BillingMode.prepay),
                    session,
                    operator,
                )
                converted_payg_accounts += 1
            except HTTPException as exc:
                # Partial failure is RESUMABLE, not rolled back: accounts
                # already settled+converted are genuinely converted (their
                # charges are real and their meters were really reset), and a
                # retry skips them (no longer payg). Name the account so the
                # operator knows exactly where it stopped.
                raise HTTPException(
                    exc.status_code,
                    f"تبدیل اکانت {account.marzban_username} نشست: {exc.detail} "
                    "— اکانت‌های قبلی تبدیل شده‌اند؛ دوباره تلاش کن، از همان‌جا ادامه می‌دهد.",
                ) from exc

    shop_user = get_or_create_shop_user(
        session,
        body.telegram_id,
        telegram_username=body.telegram_username,
        display_name=body.display_name,
    )

    if customer.shop_user_id is not None:
        if customer.shop_user_id == shop_user.id:
            # Same pair — idempotent: refresh the display snapshots and
            # re-count, no state change, still worth a DM attempt (it may
            # succeed NOW if the customer has since pressed /start).
            accounts_linked = _linked_account_count(session, customer)
            delivered, note = await _send_bind_confirmation(shop_user.telegram_id, customer.name, accounts_linked)
            return ShopTelegramBindResult(
                customer_id=customer.id,
                customer_name=customer.name,
                linked_telegram_id=shop_user.telegram_id,
                accounts_linked=accounts_linked,
                notification_delivered=delivered,
                notification_note=note,
                converted_payg_accounts=converted_payg_accounts,
                settled_amount=round(settled_amount, 2),
            )
        other = session.get(ShopUser, customer.shop_user_id)
        bound = f"telegram id {other.telegram_id}" if other else "an unknown shop user"
        raise HTTPException(
            409,
            f"این مشتری از قبل به تلگرامِ دیگری وصل است ({bound}) — اول با "
            f"DELETE /api/shop/link/{customer.id} جدا کنید، بعد دوباره ببندید.",
        )

    # One Telegram account MAY carry several customers (owner 2026-10-26: one
    # person buys for the whole family) - no «id already linked elsewhere» refusal.
    customer.shop_user_id = shop_user.id
    customer.shop_link_linked_at = utcnow()
    # A pending invite is superseded by a direct bind: its token dies with
    # the link, so a claim arriving later finds nothing and 404s. Same for a
    # pending bind-by-identity claim: the operator just did the bind by hand,
    # so the claim has done its job and must not linger looking like work.
    customer.shop_link_token = None
    customer.shop_link_expires_at = None
    for stale_claim in session.exec(
        select(ShopBindClaim).where(ShopBindClaim.customer_id == customer.id, ShopBindClaim.claimed_at.is_(None))
    ).all():
        session.delete(stale_claim)
    session.add(customer)
    session.commit()
    session.refresh(customer)
    accounts_linked = _linked_account_count(session, customer)
    delivered, note = await _send_bind_confirmation(shop_user.telegram_id, customer.name, accounts_linked)
    return ShopTelegramBindResult(
        customer_id=customer.id,
        customer_name=customer.name,
        linked_telegram_id=shop_user.telegram_id,
        accounts_linked=accounts_linked,
        notification_delivered=delivered,
        notification_note=note,
        converted_payg_accounts=converted_payg_accounts,
        settled_amount=round(settled_amount, 2),
    )


async def _send_bind_confirmation(telegram_id: int, customer_name: str, accounts_linked: int) -> tuple[bool, Optional[str]]:
    """Best-effort «شما وصل شدید» DM after a direct bind. Doubles as the
    operator's typo check: a wrong id (nobody home) or a customer who has
    never started the bot both surface here as notification_delivered=False
    with the reason spelled out — the bind itself is never undone by it.
    send_to_shop_user raises on ANY failure and returns None on success, so
    reaching the return line IS the delivery."""
    from app.notify import send_to_shop_user

    text = (
        "🔗 حساب شما به فروشگاه وصل شد!\n\n"
        "از این به بعد «سرویس‌های من» سرویس‌هایتان را نشان می‌دهد و می‌توانید "
        "کیف پولتان را شارژ کنید و سرویستان را تمدید کنید."
        + (f"\n\nتعداد سرویس‌های متصل: {accounts_linked}" if accounts_linked else "")
    )
    try:
        await send_to_shop_user(telegram_id, text)
        return True, None
    except RuntimeError as exc:
        return False, str(exc)[:200]
    except Exception as exc:  # noqa: BLE001 — the DM must never fail the bind
        return False, f"Telegram refused the confirmation message: {exc}"


# ══════════════════════════════════════════════════════ shop-bot endpoints


def _normalize_claim_username(value: Optional[str]) -> Optional[str]:
    """@Ali / ali / ALI all store as "ali" — Telegram usernames are
    case-insensitive and the operator will type them every possible way."""
    if value is None:
        return None
    v = value.strip().lstrip("@").lower()
    return v or None


def _normalize_claim_display(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = " ".join(value.split()).casefold()
    return v or None


def _bind_guards(customer: Customer) -> Optional[str]:
    """The direct-bind refusals, shared verbatim by the claim endpoints and
    the session hook — a claim that could never legally bind must not sit
    pending looking like work waiting to happen. Returns the refusal
    sentence, or None when the customer is claimable."""
    if customer.is_group_rep:
        return (
            "این مشتری نمایندهٔ گروه/خانواده است — اتصالِ سرویس‌های مشترکِ گروه به یک "
            "تلگرامِ شخصی یعنی یک نفر همه را ببیند و تمدید کند. برای این حالت، دسترسیِ "
            "دلیگیت جایگزین است."
        )
    return None


def _try_complete_bind_claim(session: Session, shop_user: ShopUser) -> None:
    """The session hook that makes «بایند با آیدی» work: the bot reports its
    own numeric identity on EVERY /session call (bot/handlers/shop.py's
    _session sends telegram_id + username + display_name), so the first
    message from the named customer completes the bind — no operator round
    trip, no numeric id ever asked for.

    Strict on purpose: exactly ONE pending claim may match (two «علی»s bind
    nothing), the direct-bind guards re-run at completion time (state may
    have changed since the claim was written), and a ShopUser already bound
    to another customer is never stolen. A claim that can no longer legally
    bind (customer linked by hand meanwhile, turned group rep, gained payg
    billing) is DELETED rather than left pending — a pending list that never
    completes is a lie the operator re-reads every day. Runs synchronously
    inside the session request; any failure here must never break the bot's
    menu, hence the blanket except with a log."""
    import logging

    logger = logging.getLogger("app.shop.bind_claim")
    try:
        username = _normalize_claim_username(shop_user.telegram_username)
        display = _normalize_claim_display(
            " ".join(filter(None, [shop_user.display_name])) or None
        ) if shop_user.display_name else None
        if username is None and display is None:
            return
        from sqlalchemy import or_

        conds = []
        if username:
            conds.append(ShopBindClaim.telegram_username == username)
        if display:
            conds.append(ShopBindClaim.display_name == display)
        pending = session.exec(
            select(ShopBindClaim).where(ShopBindClaim.claimed_at.is_(None), or_(*conds))
        ).all()
        if not pending:
            return
        customer_ids = {c.customer_id for c in pending}
        if len(customer_ids) != 1:
            # Ambiguous (two claims match this identity) — bind nothing, tell
            # the operator via the log; the pending list still shows both.
            logger.warning("bind-claim ambiguous for shop user %s (%s candidates)", shop_user.telegram_id, len(pending))
            return
        customer = session.get(Customer, customer_ids.pop())
        if customer is None:
            for c in pending:
                session.delete(c)
            session.commit()
            return
        refusal = _bind_guards(customer)
        payg_standalone, payg_groups = _payg_partition(session, customer)
        if refusal is None and payg_groups:
            refusal = "بیلینگ مصرفی از گروه — حالت گروه را prepay کن."
        if refusal is None and payg_standalone and customer.shop_user_id is None:
            # ARRIVAL, not a bind (council 2026-10-25): a payg customer is never
            # linked/converted by a bot message. Record WHO wrote - the numeric
            # id comes from the sender's own Telegram client, so it is the
            # person holding that @username right now, not a stale snapshot -
            # and leave the claim pending. The panel then offers the operator
            # the one-click settle+convert+link for exactly this id.
            for c in pending:
                if c.customer_id == customer.id:
                    c.claimed_telegram_id = shop_user.telegram_id
                    session.add(c)
            session.commit()
            logger.info(
                "bind-claim ARRIVED (payg, awaiting operator): customer %s <- telegram %s",
                customer.id, shop_user.telegram_id,
            )
            return
        if refusal is not None:
            logger.warning("bind-claim for customer %s no longer legal (%s) — discarded", customer.id, refusal[:60])
            for c in pending:
                if c.customer_id == customer.id:
                    session.delete(c)
            session.commit()
            return
        if customer.shop_user_id is not None:
            # The operator bound this customer by hand while the claim sat
            # pending — the manual bind wins, the claim has done its time.
            for c in pending:
                if c.customer_id == customer.id:
                    session.delete(c)
            session.commit()
            return
        customer.shop_user_id = shop_user.id
        customer.shop_link_linked_at = utcnow()
        customer.shop_link_token = None
        customer.shop_link_expires_at = None
        session.add(customer)
        for c in pending:
            if c.customer_id == customer.id:
                c.claimed_at = utcnow()
                c.claimed_telegram_id = shop_user.telegram_id
                session.add(c)
        session.commit()
        session.refresh(customer)
        logger.info(
            "bind-claim COMPLETED: customer %s (%s) -> telegram %s via %s",
            customer.id, customer.name, shop_user.telegram_id,
            "username" if username else "display name",
        )
    except Exception:
        session.rollback()
        logger.exception("bind-claim completion failed (bot session continues)")


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
    # Bind-by-identity hook (see _try_complete_bind_claim): the operator's
    # pending claim, if this identity matches it, completes HERE — the bot
    # itself just supplied the numeric id. The menu the customer is about to
    # see is built AFTER this, so «سرویس‌های من» already shows their services
    # on this very message.
    _try_complete_bind_claim(session, user)
    settings = get_shop_settings(session)
    # Routed-books debt view (council 2026-10-24): read-only, from the ledger.
    ledger_debt = _linked_ledger_debt(session, user)
    from app.shop_service import linked_debt_by_customer

    split = linked_debt_by_customer(session, user.id)
    owing = [{"name": c.name, "debt": round(d, 2)} for c, d in split if d > 0.5]
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
        phone=user.phone,
        payg_enabled=bool(settings.payg_enabled and settings.payg_rate_per_gb),
        payg_rate_per_gb=settings.payg_rate_per_gb or 0,
        ledger_debt=ledger_debt,
        # whose debt it is matters as soon as ONE telegram carries several customers
        debt_breakdown=owing if len(split) > 1 else [],
    )


@bot_router.post("/phone")
def bot_save_phone(body: ShopBotPhoneRequest, session: Session = Depends(get_session)):
    """Stores a phone number the customer chose to share via Telegram's own
    contact-share button — never requested before their first purchase (see
    handlers/shop.py's take_trial/_confirm_wallet_purchase), and nothing
    here is ever gated on it being present."""
    user = session.exec(select(ShopUser).where(ShopUser.telegram_id == body.telegram_id)).first()
    if user is None:
        raise HTTPException(404, "Unknown shop user — call /session first")
    user.phone = body.phone
    session.add(user)
    session.commit()
    return {"ok": True}


@bot_router.post("/quote")
def bot_quote(body: ShopBotPurchaseRequest, session: Session = Depends(get_session)):
    """Price without buying. Lets the bot show a confirmation screen carrying
    the real number, rather than one the bot computed itself from a cached
    rate that may since have changed.

    Runs the SAME validation as /purchase — including the unlimited-service
    refusal, so the confirm screen never even appears for a customer whose
    service needs nothing. Quoting without validation meant the confirm
    screen would happily price a 5000 GB plan against a 200 GB maximum, or
    quote at all while the shop was closed — the customer only discovering
    it after tapping buy. A quote that cannot be honoured is worse than no
    quote.
    """
    user = session.exec(select(ShopUser).where(ShopUser.telegram_id == body.telegram_id)).first()
    if user is not None:
        try:
            refuse_unlimited_renewal(session, user.id)
        except ShopError as exc:
            raise HTTPException(409, str(exc))
    settings = get_shop_settings(session)
    try:
        validate_purchase_request(settings, body.data_limit_gb)
        return {"data_limit_gb": body.data_limit_gb, "price": quote_price(settings, body.data_limit_gb),
                "duration_days": settings.plan_duration_days}
    except ShopError as exc:
        raise HTTPException(409 if str(exc).startswith("SERVICE_IS_UNLIMITED") else 400, str(exc))


_USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{3,30}$")


def _clean_chosen_username(raw: Optional[str]) -> Optional[str]:
    """The customer-chosen Marzban username: trimmed, pattern-validated
    (letters/digits/_/-, 3-30 — Marzban's own rule). None = "auto" (shop{id}).
    Raises ShopError with a Persian sentence the bot shows verbatim."""
    name = (raw or "").strip()
    if not name:
        return None
    if not _USERNAME_PATTERN.match(name):
        raise ShopError(
            "نام کاربری فقط حروف انگلیسی، عدد و _ یا - است — بین ۳ تا ۳۰ کاراکتر."
        )
    return name


async def _ensure_username_available(name: Optional[str]) -> None:
    """Definitive pre-money availability check against the live panel: a name
    Marzban already knows is refused BEFORE any payment moves (the definitive
    guard is still create_user — a race here refunds, never double-charges)."""
    if not name:
        return
    if await marzban_client.get_user(name) is not None:
        raise ShopError("این نام کاربری قبلاً گرفته شده — یکی دیگر انتخاب کن.")


def _clean_service_name(raw: Optional[str]) -> Optional[str]:
    """The customer-typed service name: trimmed, capped at 40, empty -> None
    (never an empty string on the account). The schema caps the length too;
    this is the single enforcement point both create paths share."""
    name = (raw or "").strip()
    return name[:40] or None


@bot_router.post("/purchase", response_model=ShopPurchaseResult)
async def bot_purchase(body: ShopBotPurchaseRequest, session: Session = Depends(get_session)):
    user = session.exec(select(ShopUser).where(ShopUser.telegram_id == body.telegram_id)).first()
    if user is None:
        raise HTTPException(404, "Unknown shop user — call /session first")

    try:
        chosen = _clean_chosen_username(body.marzban_username)
    except ShopError as exc:
        raise HTTPException(400, str(exc))
    try:
        await _ensure_username_available(chosen)
    except ShopError as exc:
        raise HTTPException(409, str(exc))

    try:
        order = await purchase(session, user, body.data_limit_gb, service_name=_clean_service_name(body.service_name), marzban_username=chosen)
    except ShopError as exc:
        # A 400 with the user-facing sentence, which the bot shows verbatim.
        # The unlimited refusal AND the payg refusal (F3b) ride 409 with
        # their stable prefixes — shopbot maps each to its own Persian
        # explanation instead of raw English.
        raise HTTPException(
            409 if str(exc).startswith(("SERVICE_IS_UNLIMITED", "PAYG_TOPUP_INSTEAD")) else 400,
            str(exc),
        )

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
    except Exception:  # noqa: BLE001 — logged in full; the response stays generic
        logger.exception("Order #%s: could not deliver the QR", order_id)
        # The reason is in the log, not the response: a Telegram or httpx
        # error string carries internal hostnames and the bot's own URL.
        raise HTTPException(502, "Could not send the QR — try again in a moment")
    return {"delivered": True}


@bot_router.post("/orders", response_model=ShopOrderIntent)
async def bot_create_order(body: ShopBotPurchaseRequest, session: Session = Depends(get_session)):
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
    # Same unlimited refusal as /purchase — here it stops the RECEIPT flow
    # from ever starting for a service that needs nothing.
    try:
        refuse_unlimited_renewal(session, user.id, force_new_service=body.force_new_service)
    except ShopError as exc:
        raise HTTPException(409, str(exc))
    try:
        chosen = _clean_chosen_username(body.marzban_username)
    except ShopError as exc:
        raise HTTPException(400, str(exc))
    try:
        await _ensure_username_available(chosen)
    except ShopError as exc:
        raise HTTPException(409, str(exc))
    try:
        order = create_awaiting_order(session, user, body.data_limit_gb, force_new_service=body.force_new_service,
                                  service_name=_clean_service_name(body.service_name), marzban_username=chosen)
    except ShopError as exc:
        raise HTTPException(
            409 if str(exc).startswith(("SERVICE_IS_UNLIMITED", "PAYG_TOPUP_INSTEAD")) else 400,
            str(exc),
        )

    balance = wallet_balance(session, user.id)
    settings = get_shop_settings(session)
    creates_new = body.force_new_service or (
        renewable_account(session, user.id) is None and order.account_id is None
    )
    return ShopOrderIntent(
        order_id=order.id,
        creates_new=creates_new,
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


@bot_router.post("/orders/{order_id}/username")
async def bot_set_order_username(order_id: int, body: ShopBotServiceNameRequest,
                                 session: Session = Depends(get_session)):
    """Sets the customer-chosen Marzban username on a still-awaiting order —
    the answer to the bot's optional username question, arriving AFTER the
    order exists (the bot learns whether the purchase mints a new user from
    the intent's creates_new). Availability is checked against the live panel
    here, and again definitively at create_user (a race refunds, never
    double-charges). The order must belong to this telegram's shop user."""
    user = session.exec(select(ShopUser).where(ShopUser.telegram_id == body.telegram_id)).first()
    if user is None:
        raise HTTPException(404, "Unknown shop user — call /session first")
    order = session.get(ShopOrder, order_id)
    if order is None or order.shop_user_id != user.id:
        raise HTTPException(404, "not found")
    if order.status != ShopOrderStatus.awaiting_payment:
        raise HTTPException(409, "This order is already past the username step")
    try:
        chosen = _clean_chosen_username(body.name)
        if chosen:
            await _ensure_username_available(chosen)
    except ShopError as exc:
        # Invalid pattern -> 400, taken -> 409; both carry the Persian
        # sentence the bot shows verbatim so the customer can retry IN the
        # ask (the state stays open on a 409).
        raise HTTPException(409 if "قبلاً گرفته شده" in str(exc) else 400, str(exc))
    order.requested_username = chosen
    session.add(order)
    session.commit()
    return {"ok": True, "requested_username": order.requested_username}


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


@bot_router.post("/accounts/{account_id}/auto-renew", response_model=ShopAutoRenewResult)
async def bot_auto_renew_account(account_id: int, body: ShopBotOrderAction, session: Session = Depends(get_session)):
    """The "⚡ تمدید خودکار (میانگین مصرفم)" button on the usage warning —
    renews this account by its own average volume, debiting the wallet even
    past zero (the one purchase path allowed to do that; see
    shop_service.auto_renew_account's docstring for the bounded-debt design).
    """
    user = session.exec(select(ShopUser).where(ShopUser.telegram_id == body.telegram_id)).first()
    if user is None:
        raise HTTPException(404, "Unknown shop user — call /session first")
    try:
        result = await auto_renew_account(session, account_id, user)
    except ShopError as exc:
        raise HTTPException(409 if str(exc).startswith(("ALREADY_IN_DEBT", "NO_HISTORY")) else 400, str(exc))

    order = result["order"]
    if order.status == ShopOrderStatus.failed:
        raise HTTPException(502, order.error or "Could not renew the account")

    await deliver_order_to_customer(session, order)

    account = session.get(Account, order.account_id) if order.account_id else None
    owed = max(0, -result["balance"])
    return ShopAutoRenewResult(
        order_id=order.id,
        marzban_username=order.marzban_username,
        data_limit_gb=order.data_limit_gb,
        duration_days=order.duration_days,
        price=order.price,
        balance=result["balance"],
        subscription_url=resolve_subscription_url(account.subscription_url) if account else None,
        status=order.status,
        went_negative=result["went_negative"],
        owed=owed,
        grace_days=DEBT_SUSPEND_AFTER_DAYS,
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


@bot_router.post("/payg/start", response_model=ShopPaygStartResult)
async def bot_start_payg(body: ShopPaygStartRequest, session: Session = Depends(get_session)):
    """The "📊 سرویس مصرفی" button: starts a metered account with no fixed
    volume, no money up front. See shop_service.start_payg_service's
    docstring for the billing design.
    """
    user = get_or_create_shop_user(
        session,
        body.telegram_id,
        telegram_username=body.telegram_username,
        display_name=body.display_name,
    )
    try:
        chosen = _clean_chosen_username(body.marzban_username)
        await _ensure_username_available(chosen)
    except ShopError as exc:
        # Invalid pattern -> 400, taken -> 409; both carry the Persian
        # sentence the bot shows verbatim so the customer can retry.
        raise HTTPException(409 if "قبلاً گرفته شده" in str(exc) else 400, str(exc))
    try:
        order = await start_payg_service(session, user, marzban_username=chosen)
    except ShopError as exc:
        raise HTTPException(
            409 if str(exc).startswith(("PAYG_NOT_AVAILABLE", "ALREADY_HAS_SERVICE")) else 400, str(exc),
        )

    if order.status == ShopOrderStatus.failed:
        raise HTTPException(502, order.error or "Could not start the service")

    await deliver_order_to_customer(session, order)

    account = session.get(Account, order.account_id) if order.account_id else None
    return ShopPaygStartResult(
        order_id=order.id,
        marzban_username=order.marzban_username,
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
    # Checked BEFORE creating this one, against everything that already
    # exists — the topup this request is about to make doesn't count as a
    # prior use of its own text.
    reused = find_prior_receipt_text_use(session, body.receipt_text)
    try:
        topup = create_topup(session, user, body.claimed_amount, body.receipt_file_id,
                             order_id=body.order_id, receipt_text=body.receipt_text,
                             for_debt=body.for_debt)
    except ShopConflict as exc:
        raise HTTPException(409, str(exc))
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
    # Ordered deliberately: the operator is alerted first, then the bridge
    # service is built. If Marzban is having a bad minute, the receipt is
    # still in front of the operator.
    background_tasks.add_task(_grant_bridge_service, topup.id)
    background_tasks.add_task(_alert_operator_to_topup, topup.id, user.telegram_id,
                              user.display_name or user.telegram_username, body.receipt_file_id,
                              topup.claimed_amount, topup.reference_code, order_summary,
                              body.receipt_text, reused.id if reused else None,
                              body.for_debt)

    return ShopTopupRead(**topup.model_dump(), telegram_id=user.telegram_id, display_name=user.display_name)


@bot_router.post("/pay-debt-from-wallet", response_model=ShopBotPayDebtResult)
async def bot_pay_debt_from_wallet(
    body: ShopBotPayDebtRequest,
    session: Session = Depends(get_session),
):
    """Pay the linked customer's reseller-ledger debt OUT of the shop wallet —
    the single sanctioned cross-system write (council 2026-10-24: explicit,
    customer-initiated, double-referenced). Both rows post in ONE transaction
    under the shop's per-user lock; the amount is capped at min(request,
    wallet, debt) re-read inside the lock, so a double-tap can never overpay
    (the second call finds debt or wallet exhausted and refuses cleanly)."""
    user = session.exec(select(ShopUser).where(ShopUser.telegram_id == body.telegram_id)).first()
    if user is None:
        raise HTTPException(404, "Unknown shop user — call /session first")
    if user.is_blocked:
        raise HTTPException(403, "This account can't move money. Contact support.")

    from app.models import Customer, LedgerEntry, LedgerSource, LedgerType
    from app.services import billing_lock

    from app.shop_service import allocate_debt_payment, linked_customers, linked_debt

    if not linked_customers(session, user.id):
        raise HTTPException(400, "این حساب بدهیِ صورت‌حسابی ندارد (به مشتری وصل نیست).")

    async with billing_lock:
        wallet = wallet_balance(session, user.id)
        debt = linked_debt(session, user.id)
        pay = min(body.amount if body.amount is not None else wallet, wallet, int(debt))
        if wallet <= 0:
            raise HTTPException(400, "کیف پولتان خالی است — برای پرداخت بدهی رسید بفرستید (گزینهٔ «پرداخت بدهی»).")
        if debt <= 0:
            raise HTTPException(400, "بدهیِ صورت‌حسابی ندارید.")
        if pay < 1000:
            raise HTTPException(400, "مبلغ پرداخت باید حداقل ۱٬۰۰۰ تومان باشد.")

        ref = f"debt-pay:shopuser:{user.id}:wallet:{wallet}:debt:{int(debt)}"
        session.add(ShopWalletEntry(
            shop_user_id=user.id,
            type=ShopWalletEntryType.debt_pay,
            amount=-pay,
            note=f"پرداخت بدهی صورت‌حساب — {ref}",
        ))
        # pay <= total debt, so the split never exceeds any one customer's debt
        # (allocate_debt_payment: oldest customer first, each capped at what they owe).
        for customer_id, part in allocate_debt_payment(session, user.id, float(pay)):
            session.add(LedgerEntry(
                type=LedgerType.credit,
                amount=float(part),
                customer_id=customer_id,
                note=f"پرداخت بدهی از کیف پول فروشگاه — {ref}",
                source=LedgerSource.shop,
                created_by="system:shop-debt-pay",
            ))
        session.commit()

    wallet_after = wallet_balance(session, user.id)
    debt_after = linked_debt(session, user.id)
    return ShopBotPayDebtResult(paid=pay, wallet_balance=wallet_after, ledger_debt=round(debt_after, 2))


async def _grant_bridge_service(topup_id: int) -> None:
    """Runs after the customer has been told their receipt arrived.

    Its own session because a background task outlives the request's. Every
    failure is swallowed on purpose: the payment stands with or without this,
    and an exception here would only surface as a 500 on a request that has
    already succeeded.
    """
    try:
        with Session(engine) as session:
            topup = session.get(ShopTopup, topup_id)
            if topup is None or topup.order_id is None:
                return
            user = session.get(ShopUser, topup.shop_user_id)
            order = session.get(ShopOrder, topup.order_id)
            if user is None or order is None:
                return
            await maybe_grant_provisional(session, user, order, topup.claimed_amount)
    except Exception:
        logger.exception("Bridge service for top-up #%s failed", topup_id)


async def _alert_operator_to_topup(
    topup_id: int,
    telegram_id: int,
    who: Optional[str],
    receipt_file_id: Optional[str],
    claimed_amount: int,
    reference_code: Optional[str] = None,
    order_summary: Optional[str] = None,
    receipt_text: Optional[str] = None,
    for_debt: bool = False,
    reused_from_topup_id: Optional[int] = None,
) -> None:
    # The reference code is shown because the customer was given it and will
    # quote it back. The order line matters more: approving an order-bound
    # payment also DELIVERS a plan, and approving less than it costs leaves the
    # customer waiting for one — the operator needs to know which kind of
    # approval they are making before they tap.
    ref = f" · code {reference_code}" if reference_code else ""
    kind = (
        (f"\nFor: {order_summary} (approving delivers it)" if order_summary else "\nFor: wallet credit only")
        if not for_debt
        else "\nFor: DEBT PAYMENT — approving posts a LEDGER credit, the wallet is untouched"
    )
    # Plain text throughout this module (see notify.py) — no parse_mode is
    # ever set, so nothing the customer typed can be interpreted as
    # formatting. It's shown quoted only for visual separation from the
    # operator's own lines, not because it needs escaping.
    receipt_line = f"\n📝 Typed receipt: “{receipt_text}”" if receipt_text else ""
    dupe_line = (
        f"\n⚠️ This exact text was already used on top-up #{reused_from_topup_id} — check before approving."
        if reused_from_topup_id else ""
    )
    caption = (
        f"💳 New payment #{topup_id}{ref}\n"
        f"From: {who or 'unknown'} (id {telegram_id})\n"
        f"Claimed: {claimed_amount:,} T"
        f"{kind}{receipt_line}{dupe_line}"
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


@bot_router.post("/claim-link", response_model=ShopBotClaimLinkResult)
def bot_claim_link(body: ShopBotClaimLinkRequest, session: Session = Depends(get_session)):
    """The other half of the operator's shop-link invite: the customer tapped
    t.me/<shop_bot>?start=shoplnk_<token>, shopbot relayed the token, and
    THIS is where the customer row is bound to their Telegram identity.
    Auth is the shop bot key, not the customer — the customer's only
    credential is unguessable possession of the token itself, exactly the
    delegate claim's trust model (routers/delegate.py bot_claim).

    404 = token unknown, already consumed, or expired — all three read
    identically to a holder of a stale link (no oracle telling an attacker
    which tokens ever existed). 409 = the binding would contradict one that
    already exists, in either direction (this customer is already bound to a
    different Telegram, or this Telegram is already bound to a different
    customer)."""
    token = body.token.strip()
    customer = session.exec(
        select(Customer).where(Customer.shop_link_token == token)
    ).first() if token else None
    # SQLite DATETIME columns store naive UTC here (same convention the rest
    # of this codebase compares against — see routers/delegate.py's claim),
    # so the comparison is naive-vs-naive on purpose.
    now = utcnow().replace(tzinfo=None)
    if (
        customer is None
        or customer.shop_link_expires_at is None
        or customer.shop_link_expires_at <= now
    ):
        raise HTTPException(404, "This invite link is invalid or has expired — ask the operator for a fresh one")

    shop_user = get_or_create_shop_user(
        session,
        body.telegram_id,
        telegram_username=body.telegram_username,
    )

    # Defensive idempotency: a customer whose token is STILL on the row but
    # who already has a link can only mean the same person claiming twice in
    # a race — answering success twice costs nothing, so it does.
    if customer.shop_user_id is not None:
        if customer.shop_user_id == shop_user.id:
            return ShopBotClaimLinkResult(
                customer_name=customer.name,
                accounts_linked=_linked_account_count(session, customer),
            )
        raise HTTPException(
            409,
            "This customer is already linked to a different Telegram account — "
            "ask the operator to unlink it first",
        )

    # One Telegram account MAY carry several customers (owner 2026-10-26: one
    # person buys for the whole family) - no «id already linked elsewhere» refusal.
    customer.shop_user_id = shop_user.id
    customer.shop_link_linked_at = utcnow()
    customer.shop_link_token = None
    customer.shop_link_expires_at = None
    session.add(customer)
    session.commit()
    session.refresh(customer)
    return ShopBotClaimLinkResult(
        customer_name=customer.name,
        accounts_linked=_linked_account_count(session, customer),
    )


@bot_router.get("/accounts", response_model=list[ShopBotAccountRow])
def bot_list_accounts(telegram_id: int, session: Session = Depends(get_session)):
    """The customer's own accounts, with live-ish usage from the last sync.
    Scoped by their orders — never by a name pattern, which would hand
    someone else's account to anyone who guessed a username.

    Two sources, merged and deduped by account id: delivered SHOP orders
    (source "shop", order_id set), and — for a customer linked via the
    operator's invite — the accounts they own directly outside the shop
    (source "linked", order_id None). Linked accounts are filtered to the
    same visibility the operator's own panel gives the customer: a disabled
    account still SHOWS (as disabled — hiding it would read as the service
    vanishing), but one deleted from Marzban is gone and shows nothing. An
    account reachable both ways (an operator-linked customer who later bought
    its renewal through the shop) appears once, as the shop row — that one
    carries the order context the pure mirror doesn't have."""
    user = session.exec(select(ShopUser).where(ShopUser.telegram_id == telegram_id)).first()
    if user is None:
        return []
    orders = session.exec(
        select(ShopOrder)
        .where(ShopOrder.shop_user_id == user.id, ShopOrder.status == ShopOrderStatus.delivered)
        .order_by(ShopOrder.created_at.desc())
    ).all()

    rows: list[ShopBotAccountRow] = []
    seen_account_ids: set[int] = set()
    for order in orders:
        account = session.get(Account, order.account_id) if order.account_id else None
        if account is not None:
            # orders are newest-first, so the first order that reaches a given
            # account IS its most recent one - every later (older) order for
            # the same account is a prior purchase/top-up on the SAME service,
            # not a second service, and must not get its own duplicate row
            # (live 2026-10-22: a top-up order showed the account twice).
            if account.id in seen_account_ids:
                continue
            seen_account_ids.add(account.id)
            # Gone from the panel = gone for the customer, exactly like the
            # linked loop below (live 2026-10-26: operator-deleted «shop10» kept
            # showing as «30 GB, 28 days left»). Added to seen first so an
            # older order of the same deleted account is skipped too.
            if account.status == "deleted_from_marzban" or account.deleted_at is not None:
                continue
        rows.append(ShopBotAccountRow(
            order_id=order.id,
            source="shop",
            account_id=account.id if account else None,
            service_name=account.service_name if account else None,
            marzban_username=order.marzban_username or "—",
            data_limit_gb=order.data_limit_gb,
            used_traffic=account.used_traffic if account else 0,
            data_limit=account.data_limit if account else None,
            expire=account.expire if account else None,
            status=account.status if account else None,
            subscription_url=resolve_subscription_url(account.subscription_url) if account else None,
            created_at=order.created_at,
        ))

    # EVERY customer linked to this Telegram (one person may hold the whole
    # family's services - owner 2026-10-26), their accounts merged.
    from app.shop_service import linked_customers

    customer_ids = [c.id for c in linked_customers(session, user.id)]
    if customer_ids:
        linked = session.exec(
            select(Account)
            .where(Account.customer_id.in_(customer_ids))
            .order_by(Account.created_at.desc(), Account.id.desc())
        ).all()
        for account in linked:
            if account.id in seen_account_ids:
                continue
            if account.status == "deleted_from_marzban":
                continue
            rows.append(ShopBotAccountRow(
                order_id=None,
                source="linked",
                account_id=account.id,
                service_name=account.service_name,
                marzban_username=account.marzban_username,
                # A GB figure for consistency with shop rows; the bot's own
                # list rendering reads data_limit (bytes), not this.
                data_limit_gb=round(account.data_limit / (1024 ** 3), 2) if account.data_limit else 0.0,
                used_traffic=account.used_traffic,
                data_limit=account.data_limit,
                expire=account.expire,
                status=account.status,
                subscription_url=resolve_subscription_url(account.subscription_url),
                created_at=account.created_at,
            ))
    return rows


@bot_router.post("/accounts/{account_id}/name")
async def bot_rename_account(account_id: int, body: ShopBotServiceNameRequest,
                             session: Session = Depends(get_session)):
    """The customer names THEIR OWN service («گوشی علی»). Ownership is the
    same scoping bot_list_accounts uses — the account must be a delivered
    shop order of this telegram's shop user, or an account of the customer
    linked to it; anything else is 404 with no oracle about whose it is.
    An empty/whitespace name clears the nickname."""
    user = session.exec(select(ShopUser).where(ShopUser.telegram_id == body.telegram_id)).first()
    if user is None:
        raise HTTPException(404, "Unknown shop user — call /session first")
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(404, "not found")

    visible_ids: set[int] = set()
    orders = session.exec(
        select(ShopOrder).where(ShopOrder.shop_user_id == user.id,
                                ShopOrder.status == ShopOrderStatus.delivered)
    ).all()
    for order in orders:
        if order.account_id:
            visible_ids.add(order.account_id)
    from app.shop_service import linked_customers

    for linked_customer in linked_customers(session, user.id):
        for acc in session.exec(select(Account.id).where(Account.customer_id == linked_customer.id)).all():
            visible_ids.add(acc if isinstance(acc, int) else acc[0])

    if account_id not in visible_ids:
        raise HTTPException(404, "not found")

    name = (body.name or "").strip()
    if len(name) > 40:
        raise HTTPException(400, "The name is limited to 40 characters")
    old = account.service_name
    account.service_name = name or None
    session.add(account)
    session.add(AccountEvent(
        account_id=account.id,
        action="service_renamed",
        detail=f"{old!r} -> {account.service_name!r} (set by the customer in the shop bot)",
        created_by=f"telegram:{body.telegram_id}",
    ))
    session.commit()
    return {"ok": True, "service_name": account.service_name}


@router.get("/issues")
def list_shop_issues(session: Session = Depends(get_session)):
    """The operator's provisioning-failure queue (owner request 2026-10-23):
    every order where a PAYING customer did not get their service, in one
    glance. Two sections:
      stuck  — status `provisioning` past the sweeper's timeout; the sweep
               will resolve these (deliver or refund) within minutes, but
               while one sits here a customer is waiting with money held.
      failed — provisioned and failed; the wallet was automatically
               refunded (every failed path refunds — that is the design),
               so the money is safe and the row is here so the operator can
               see WHY (the raw Marzban error) and reach the customer.
    Failed orders are capped at the newest 50; this is an attention queue,
    not an archive — the full history lives in the Orders tab."""
    stuck_before = utcnow() - timedelta(minutes=STUCK_ORDER_TIMEOUT_MINUTES)
    stuck = session.exec(
        select(ShopOrder)
        .where(ShopOrder.status == ShopOrderStatus.provisioning,
               ShopOrder.created_at < stuck_before)
        .order_by(ShopOrder.created_at)
    ).all()
    failed = session.exec(
        select(ShopOrder)
        .where(ShopOrder.status == ShopOrderStatus.failed)
        .order_by(ShopOrder.created_at.desc())
        .limit(50)
    ).all()

    def row(o: ShopOrder) -> dict:
        user = session.get(ShopUser, o.shop_user_id)
        return {
            "id": o.id,
            "customer": (user.display_name or user.telegram_username or str(user.telegram_id)) if user else "—",
            "telegram_id": user.telegram_id if user else None,
            "price": o.price,
            "data_limit_gb": o.data_limit_gb,
            "error": o.error,
            "created_at": o.created_at,
        }

    return {
        "stuck": [row(o) for o in stuck],
        "failed": [row(o) for o in failed],
        "counts": {"stuck": len(stuck), "failed": len(failed)},
    }


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
