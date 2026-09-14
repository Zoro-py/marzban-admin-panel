"""The self-serve shop's money and provisioning logic.

Read the "self-serve shop" section header in models.py before changing
anything here — it explains why this is a second, separate ledger rather than
reuse of LedgerEntry, and that mixing the two corrupts every reseller-side
balance in the dashboard.

Blast radius (AGENTS.md §6): HIGH. Every function below either moves a
customer's prepaid money or creates a Marzban user.

THE ORDER OF OPERATIONS IN purchase() IS THE WHOLE DESIGN. Do not reorder it
without reading the comment on that function.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app.marzban_client import MarzbanAuthError, MarzbanUnavailable, marzban_client
from app.models import (
    Account,
    AccountEvent,
    ShopOrder,
    ShopOrderStatus,
    ShopSettings,
    ShopTopup,
    ShopTopupStatus,
    ShopUser,
    ShopWalletEntry,
    ShopWalletEntryType,
    utcnow,
)
from app.services import bytes_from_gb

logger = logging.getLogger(__name__)

SECONDS_IN_DAY = 86400

# An order left in `provisioning` longer than this is presumed dead — the
# process was killed between taking the money and creating the account — and
# is refunded by sweep_stuck_orders below. Comfortably longer than any real
# Marzban call (the client's own timeout is 20s) so a merely slow panel never
# trips it.
STUCK_ORDER_TIMEOUT_MINUTES = 10

# Per-user purchase serialisation. Two taps on "buy" arriving together would
# otherwise both read the same balance and both pass the affordability check,
# letting one wallet fund two orders.
#
# THIS IS AN IN-PROCESS LOCK AND IT IS ONLY SUFFICIENT BECAUSE THE BACKEND
# RUNS AS A SINGLE PROCESS (backend/Dockerfile starts uvicorn with no
# --workers, so there is one event loop). If this is ever deployed with
# multiple workers or replicas, this lock silently stops protecting anything
# and the balance check has to move into the database — SELECT ... FOR UPDATE
# on Postgres, or BEGIN IMMEDIATE on SQLite. Nothing here will fail loudly if
# that day comes, which is exactly why it is written down.
_purchase_locks: dict[int, asyncio.Lock] = {}


def _lock_for(shop_user_id: int) -> asyncio.Lock:
    lock = _purchase_locks.get(shop_user_id)
    if lock is None:
        lock = asyncio.Lock()
        _purchase_locks[shop_user_id] = lock
    return lock


class ShopError(Exception):
    """Something the END USER should be told, in their own words — an
    insufficient balance, a closed shop, an out-of-range volume. Distinct from
    an unexpected exception, which the bot reports as a generic failure and
    the operator finds in the logs."""


# ── settings & users ──────────────────────────────────────────────────────


def get_shop_settings(session: Session) -> ShopSettings:
    settings = session.get(ShopSettings, 1)
    if settings is None:
        # Created closed (is_open defaults False) — deploying this code must
        # not put a shop live before the operator has set a price and a card
        # number. See ShopSettings.is_open.
        settings = ShopSettings(id=1)
        session.add(settings)
        session.commit()
        session.refresh(settings)
    return settings


def get_or_create_shop_user(
    session: Session,
    telegram_id: int,
    *,
    telegram_username: Optional[str] = None,
    display_name: Optional[str] = None,
) -> ShopUser:
    """Looked up by telegram_id only. Username and display name are refreshed
    on every call because both can change on Telegram's side, but neither is
    ever used to identify anyone — someone who changes their @handle must stay
    the same wallet."""
    user = session.exec(select(ShopUser).where(ShopUser.telegram_id == telegram_id)).first()
    now = utcnow()
    if user is None:
        user = ShopUser(
            telegram_id=telegram_id,
            telegram_username=telegram_username,
            display_name=display_name,
            last_seen_at=now,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user

    if telegram_username is not None:
        user.telegram_username = telegram_username
    if display_name is not None:
        user.display_name = display_name
    user.last_seen_at = now
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# ── wallet ────────────────────────────────────────────────────────────────


def wallet_balance(session: Session, shop_user_id: int) -> int:
    """Always the SUM of the ledger, never a stored field.

    Amounts are signed, so this is a plain sum with no per-type branching —
    which means a wallet entry type added later cannot accidentally be left
    out of the balance by a branch nobody updated.
    """
    total = session.exec(
        select(func.sum(ShopWalletEntry.amount)).where(ShopWalletEntry.shop_user_id == shop_user_id)
    ).one()
    return int(total or 0)


def post_wallet_entry(
    session: Session,
    shop_user_id: int,
    *,
    entry_type: ShopWalletEntryType,
    amount: int,
    note: Optional[str] = None,
    topup_id: Optional[int] = None,
    order_id: Optional[int] = None,
    commit: bool = True,
) -> ShopWalletEntry:
    """Appends one signed entry. `commit=False` lets a caller write this in
    the SAME transaction as whatever it pays for — which is how purchase()
    guarantees an order and its debit can never exist without each other."""
    entry = ShopWalletEntry(
        shop_user_id=shop_user_id,
        type=entry_type,
        amount=amount,
        note=note,
        topup_id=topup_id,
        order_id=order_id,
    )
    session.add(entry)
    if commit:
        session.commit()
        session.refresh(entry)
    return entry


# ── pricing ───────────────────────────────────────────────────────────────


def quote_price(settings: ShopSettings, data_limit_gb: float) -> int:
    """Whole Toman for `data_limit_gb` at the shop's current retail rate.

    Rounded UP, not to nearest: rounding down means selling a fraction of a
    gigabyte for nothing, repeatedly, on every order. The difference is at
    most a few Toman per sale and always in the operator's favour, which is
    the correct direction for a price to be wrong in.
    """
    if settings.price_per_gb <= 0:
        raise ShopError("Shop pricing isn't configured yet.")
    exact = data_limit_gb * settings.price_per_gb
    return int(-(-exact // 1))  # ceil, without importing math for one call


def validate_purchase_request(settings: ShopSettings, data_limit_gb: float) -> None:
    """Raises ShopError with a message meant for the end user."""
    if not settings.is_open:
        raise ShopError("The shop is closed right now.")
    if data_limit_gb < settings.min_gb:
        raise ShopError(f"The smallest plan is {settings.min_gb:g} GB.")
    if data_limit_gb > settings.max_gb:
        raise ShopError(f"The largest plan is {settings.max_gb:g} GB.")


# ── purchase ──────────────────────────────────────────────────────────────


async def purchase(session: Session, shop_user: ShopUser, data_limit_gb: float) -> ShopOrder:
    """Buy one plan from the wallet, then provision it.

    ORDER OF OPERATIONS — this is the part that matters:

      1. Take the per-user lock, so two concurrent taps serialise.
      2. IN ONE TRANSACTION: re-read the balance, check affordability, write
         the ShopOrder and its matching negative wallet entry, commit.
         Re-reading inside the lock rather than trusting a balance read
         earlier is what stops a stale figure authorising a purchase the
         wallet can no longer cover.
      3. ONLY THEN call Marzban, outside the transaction.
      4. On success, write the Account row and mark the order delivered.
         On failure, post a compensating refund and mark the order failed.

    The money moves BEFORE Marzban is called, deliberately. The opposite order
    — provision first, charge after — loses real inventory whenever the
    process dies in between: a live account nobody paid for, indistinguishable
    from a legitimate one. This way the worst case is an order stuck in
    `provisioning` with the money held, and sweep_stuck_orders below turns
    that back into a refund automatically. A customer briefly out of pocket
    and then refunded is a recoverable state; a free account is not.
    """
    if shop_user.is_blocked:
        raise ShopError("This account can't make purchases. Contact support.")

    settings = get_shop_settings(session)
    validate_purchase_request(settings, data_limit_gb)
    price = quote_price(settings, data_limit_gb)

    async with _lock_for(shop_user.id):
        balance = wallet_balance(session, shop_user.id)
        if balance < price:
            raise ShopError(
                f"Not enough balance: this plan costs {price:,} T and your wallet has {balance:,} T."
            )

        order = ShopOrder(
            shop_user_id=shop_user.id,
            data_limit_gb=data_limit_gb,
            duration_days=settings.plan_duration_days,
            price=price,
            status=ShopOrderStatus.provisioning,
        )
        session.add(order)
        # flush assigns order.id so the debit can reference it, while keeping
        # both writes in one transaction — an order without its debit (or a
        # debit without its order) must not be reachable at any instant.
        session.flush()
        post_wallet_entry(
            session,
            shop_user.id,
            entry_type=ShopWalletEntryType.purchase,
            amount=-price,
            note=f"{data_limit_gb:g} GB / {settings.plan_duration_days} days",
            order_id=order.id,
            commit=False,
        )
        session.commit()
        session.refresh(order)

    await _provision_order(session, order, settings)
    session.refresh(order)
    return order


async def _provision_order(session: Session, order: ShopOrder, settings: ShopSettings) -> None:
    """Creates the Marzban user for an already-paid order.

    Never raises. By this point the customer's money is already gone, so an
    exception propagating up to the bot would leave them charged with no
    explanation and no refund — every outcome has to be recorded on the order
    instead.
    """
    from app.config import settings as app_settings

    username = f"{settings.username_prefix}{order.id}"
    payload = {
        "username": username,
        "proxies": app_settings.marzban_default_proxies,
        "inbounds": app_settings.marzban_default_inbounds,
        "expire": int(utcnow().timestamp()) + order.duration_days * SECONDS_IN_DAY,
        "data_limit": bytes_from_gb(order.data_limit_gb),
        "data_limit_reset_strategy": "no_reset",
        "status": "active",
        "note": f"shop order #{order.id}",
    }

    try:
        marzban_user = await marzban_client.create_user(payload)
    except (ValueError, MarzbanUnavailable, MarzbanAuthError) as exc:
        logger.exception("Shop order #%s: Marzban rejected it or was unreachable", order.id)
        refund_order(session, order, reason=str(exc))
        return

    now = utcnow()
    try:
        account = Account(
            marzban_username=username,
            used_traffic=marzban_user.get("used_traffic", 0),
            lifetime_used_traffic=marzban_user.get("lifetime_used_traffic", 0),
            first_seen_traffic=marzban_user.get("lifetime_used_traffic", 0),
            first_seen_traffic_at=now,
            usage_baseline_at=now,
            data_limit=marzban_user.get("data_limit"),
            expire=marzban_user.get("expire"),
            status=marzban_user.get("status"),
            subscription_url=marzban_user.get("subscription_url"),
            last_synced_at=now,
        )
        session.add(account)
        session.flush()
        session.add(AccountEvent(
            account_id=account.id,
            action="create",
            detail=f"Sold via shop order #{order.id}",
        ))
        order.account_id = account.id
        order.marzban_username = username
        order.status = ShopOrderStatus.delivered
        order.delivered_at = now
        order.error = None
        session.add(order)
        session.commit()
    except Exception as exc:  # noqa: BLE001 — the Marzban user exists; never silently drop this
        session.rollback()
        logger.exception("Shop order #%s: created %s in Marzban but failed to record it", order.id, username)
        # NOT refunded: the customer has a working account. Refunding here
        # would hand them the plan for free. The order is marked delivered
        # with the bookkeeping error attached, and the sync job adopts the
        # orphaned Account row on its next pass.
        order.marzban_username = username
        order.status = ShopOrderStatus.delivered
        order.delivered_at = now
        order.error = f"Delivered, but not recorded locally: {exc}"
        session.add(order)
        session.commit()


def refund_order(session: Session, order: ShopOrder, *, reason: str) -> None:
    """Returns an order's money and marks it failed — idempotent.

    The idempotency is not decoration: this is reachable from the provisioning
    path AND from the stuck-order sweeper, and those can race after a restart.
    Refunding twice would silently mint money, and since a wallet is summed
    from its entries there would be no discrepancy anywhere to notice it by.
    """
    already_refunded = session.exec(
        select(ShopWalletEntry).where(
            ShopWalletEntry.order_id == order.id,
            ShopWalletEntry.type == ShopWalletEntryType.refund,
        )
    ).first()
    if already_refunded is not None:
        logger.info("Shop order #%s already refunded; not refunding again", order.id)
        return

    post_wallet_entry(
        session,
        order.shop_user_id,
        entry_type=ShopWalletEntryType.refund,
        amount=order.price,
        note=f"Refund for failed order #{order.id}",
        order_id=order.id,
        commit=False,
    )
    order.status = ShopOrderStatus.failed
    order.error = reason
    session.add(order)
    session.commit()


def sweep_stuck_orders(session: Session) -> list[ShopOrder]:
    """Refunds orders that took the money and never reached a terminal state.

    That happens when the process dies between the wallet debit and Marzban
    responding. Without this the customer stays charged forever for nothing —
    and would have no way to tell, because from their side the bot simply
    never replied.
    """
    cutoff = utcnow() - timedelta(minutes=STUCK_ORDER_TIMEOUT_MINUTES)
    stuck = session.exec(
        select(ShopOrder).where(
            ShopOrder.status == ShopOrderStatus.provisioning,
            ShopOrder.created_at < cutoff,
        )
    ).all()
    for order in stuck:
        logger.warning("Shop order #%s stuck in provisioning since %s — refunding", order.id, order.created_at)
        refund_order(
            session,
            order,
            reason="Provisioning never completed (server restarted?) — refunded automatically",
        )
    return list(stuck)


# ── top-ups ───────────────────────────────────────────────────────────────


def create_topup(
    session: Session,
    shop_user: ShopUser,
    claimed_amount: int,
    receipt_file_id: Optional[str],
) -> ShopTopup:
    settings = get_shop_settings(session)
    if shop_user.is_blocked:
        raise ShopError("This account can't top up. Contact support.")
    if claimed_amount < settings.min_topup:
        raise ShopError(f"The smallest top-up is {settings.min_topup:,} T.")
    if claimed_amount > settings.max_topup:
        raise ShopError(f"The largest top-up is {settings.max_topup:,} T.")

    topup = ShopTopup(
        shop_user_id=shop_user.id,
        claimed_amount=claimed_amount,
        receipt_file_id=receipt_file_id,
    )
    session.add(topup)
    session.commit()
    session.refresh(topup)
    return topup


def approve_topup(session: Session, topup: ShopTopup, *, approved_amount: Optional[int] = None) -> ShopTopup:
    """Credits the wallet.

    Refuses a top-up that isn't pending. That guard is what stops a double-tap
    on the operator's approve button crediting the same receipt twice — the
    single most likely way for this system to give money away.
    """
    if topup.status != ShopTopupStatus.pending:
        raise ShopError(f"This top-up was already {topup.status.value}.")

    amount = approved_amount if approved_amount is not None else topup.claimed_amount
    if amount <= 0:
        raise ShopError("Approved amount must be positive.")

    topup.approved_amount = amount
    topup.status = ShopTopupStatus.approved
    topup.reviewed_at = utcnow()
    session.add(topup)
    post_wallet_entry(
        session,
        topup.shop_user_id,
        entry_type=ShopWalletEntryType.topup,
        amount=amount,
        note=f"Card payment approved (top-up #{topup.id})",
        topup_id=topup.id,
        commit=False,
    )
    session.commit()
    session.refresh(topup)
    return topup


def reject_topup(session: Session, topup: ShopTopup, *, reason: Optional[str] = None) -> ShopTopup:
    if topup.status != ShopTopupStatus.pending:
        raise ShopError(f"This top-up was already {topup.status.value}.")
    topup.status = ShopTopupStatus.rejected
    topup.reject_reason = reason
    topup.reviewed_at = utcnow()
    session.add(topup)
    session.commit()
    session.refresh(topup)
    return topup
