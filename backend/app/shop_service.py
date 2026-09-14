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
import secrets
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
from app.services import bytes_from_gb, sync_marzban_fields

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


def _order_note(order_id: int) -> str:
    """The marker written into the Marzban user's `note` at creation.

    This is what makes "does shop7 exist?" answerable as "did WE create shop7
    for this order?". Order ids are never reused, but an operator can still
    have made a user with the same name by hand, and adopting theirs would
    hand a stranger's account to a customer.
    """
    return f"shop order #{order_id}"


async def _find_our_marzban_user(username: str, order_id: int) -> Optional[dict]:
    """Returns the Marzban user only if WE created it for this order.

    Exists because a failed create call does NOT mean nothing was created. A
    read timeout on POST /api/user is raised as MarzbanUnavailable while the
    panel may have processed the request completely — the response is what was
    lost, not the work. Refunding on that signal alone produced a full refund
    plus a live, unbilled account: measured, not theorised.

    A lookup that itself fails returns None, which sends the caller down the
    refund path. That is the right way to be wrong: refunding a customer whose
    account does exist is recoverable by the operator, and the next sync
    surfaces the orphan account; charging for one that does not exist is not.
    """
    try:
        user = await marzban_client.get_user(username)
    except Exception:
        logger.exception("Order #%s: could not check whether %s exists in Marzban", order_id, username)
        return None
    if user is None:
        return None
    if (user.get("note") or "") != _order_note(order_id):
        logger.warning(
            "Order #%s: Marzban already has a user named %s that we did not create — not adopting it",
            order_id, username,
        )
        return None
    return user


def _record_delivered(session: Session, order: ShopOrder, username: str, marzban_user: dict) -> None:
    """Writes the local Account row and marks the order delivered.

    Refuses to do so if the order is no longer `provisioning` — the sweeper
    runs in its own session and may have refunded it while the Marzban call
    was in flight. Writing `delivered` over a refunded order is how a paid
    plan becomes a free one, with the only trace being a refund note on an
    order that says delivered.
    """
    session.expire(order)
    session.refresh(order)
    if order.status != ShopOrderStatus.provisioning:
        logger.error(
            "Order #%s reached delivery as '%s', not 'provisioning' — it was settled elsewhere "
            "(the stuck-order sweeper) while Marzban was still working. Re-charging so the "
            "delivered account is not free.",
            order.id, order.status.value,
        )
        # Deliberately allowed to take the balance negative. A negative wallet
        # is visible to the operator on the Shop page and correctable with one
        # adjustment; a delivered account that was never paid for is invisible.
        post_wallet_entry(
            session,
            order.shop_user_id,
            entry_type=ShopWalletEntryType.purchase,
            amount=-order.price,
            note=f"Re-charge: order #{order.id} was refunded but delivered anyway",
            order_id=order.id,
            commit=False,
        )

    now = utcnow()
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


async def _provision_order(
    session: Session,
    order: ShopOrder,
    settings: ShopSettings,
    *,
    duration_hours: Optional[int] = None,
) -> None:
    """Creates — or, for a returning customer, EXTENDS — the Marzban user
    for an already-paid order. See the renewal-in-place section below.

    Never raises. By this point the customer's money is already gone, so an
    exception propagating up to the bot would leave them charged with no
    explanation and no refund — every outcome has to be recorded on the order
    instead.

    `duration_hours` overrides the order's whole-day duration, and exists for
    the trial: ShopOrder.duration_days is an int, so a 6-hour trial expressed
    in days would either floor to zero (an account that is already expired
    when it is handed over) or round up to a full day the operator did not
    intend to give away. Paid plans never pass it — they are sold in days.
    """
    # A returning customer's plan is added to the account they already have,
    # so the link in their VPN app keeps working. Trials never extend — they
    # are only for people without an account (see is_existing_customer).
    if order.price > 0 and duration_hours is None:
        existing = renewable_account(session, order.shop_user_id)
        if existing is not None and await _extend_order(session, order, existing):
            return

    from app.config import settings as app_settings

    seconds = duration_hours * 3600 if duration_hours is not None else order.duration_days * SECONDS_IN_DAY
    username = f"{settings.username_prefix}{order.id}"
    payload = {
        "username": username,
        "proxies": app_settings.marzban_default_proxies,
        "inbounds": app_settings.marzban_default_inbounds,
        "expire": int(utcnow().timestamp()) + seconds,
        "data_limit": bytes_from_gb(order.data_limit_gb),
        "data_limit_reset_strategy": "no_reset",
        "status": "active",
        "note": f"shop order #{order.id}",
    }

    try:
        marzban_user = await marzban_client.create_user(payload)
    except (ValueError, MarzbanUnavailable, MarzbanAuthError) as exc:
        logger.exception("Shop order #%s: Marzban rejected it or was unreachable", order.id)
        # A failed call is NOT evidence that nothing was created. A read
        # timeout on POST /api/user loses the response, not necessarily the
        # work — and refunding on that signal alone produced a full refund
        # plus a live, unbilled account. Ask the panel before deciding.
        created = await _find_our_marzban_user(username, order.id)
        if created is not None:
            logger.warning(
                "Shop order #%s: the create call failed but %s exists in Marzban — "
                "delivering it instead of refunding", order.id, username,
            )
            try:
                _record_delivered(session, order, username, created)
            except Exception:
                session.rollback()
                logger.exception("Shop order #%s: adopted %s but could not record it", order.id, username)
                _mark_delivered_untracked(session, order, username, "adopted after a failed create")
            return
        refund_order(session, order, reason=str(exc))
        return

    try:
        _record_delivered(session, order, username, marzban_user)
    except Exception as exc:  # noqa: BLE001 — the Marzban user exists; never silently drop this
        session.rollback()
        logger.exception("Shop order #%s: created %s in Marzban but failed to record it", order.id, username)
        # NOT refunded: the customer has a working account. Refunding here
        # would hand them the plan for free. The order is marked delivered
        # with the bookkeeping error attached, and the sync job adopts the
        # orphaned Account row on its next pass.
        _mark_delivered_untracked(session, order, username, str(exc))


def _mark_delivered_untracked(session: Session, order: ShopOrder, username: str, reason: str) -> None:
    """Last resort when the Marzban user exists but the local row could not be
    written. Records the delivery so the order is never swept and refunded for
    an account the customer is actually using."""
    order.marzban_username = username
    order.status = ShopOrderStatus.delivered
    order.delivered_at = utcnow()
    order.error = f"Delivered, but not recorded locally: {reason}"
    session.add(order)
    session.commit()


def refund_order(session: Session, order: ShopOrder, *, reason: str) -> None:
    """Returns an order's money and marks it failed — idempotent.

    The idempotency is not decoration: this is reachable from the provisioning
    path AND from the stuck-order sweeper, and those can race after a restart.
    Refunding twice would silently mint money, and since a wallet is summed
    from its entries there would be no discrepancy anywhere to notice it by.
    """
    if order.status != ShopOrderStatus.provisioning:
        # Guards the state, not just the entry. The existing-refund check below
        # stops a double refund; this stops refunding an order that reached a
        # DIFFERENT terminal state — refunding a delivered account would hand
        # the customer a working plan for nothing.
        logger.warning("Shop order #%s is '%s', not 'provisioning' — refusing to refund it",
                       order.id, order.status.value)
        return

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


async def sweep_stuck_orders(session: Session) -> list[ShopOrder]:
    """Resolves orders that took the money and never reached a terminal state.

    That happens when the process dies between the wallet debit and Marzban
    responding. Without this the customer stays charged forever for nothing —
    and would have no way to tell, because from their side the bot simply
    never replied.

    "Stuck" is NOT evidence that nothing was created, which is why this asks
    the panel about every candidate before touching the money. The process can
    just as easily have died AFTER Marzban created the account, and refunding
    on age alone turned that into a free live account — the exact outcome
    purchase()'s ordering was chosen to prevent. An order whose account does
    exist is delivered late instead; only the ones the panel has never heard
    of are refunded.

    Async for that reason alone: the Marzban lookup is the whole point.
    """
    cutoff = utcnow() - timedelta(minutes=STUCK_ORDER_TIMEOUT_MINUTES)
    stuck = session.exec(
        select(ShopOrder).where(
            ShopOrder.status == ShopOrderStatus.provisioning,
            ShopOrder.created_at < cutoff,
        )
    ).all()

    settings = get_shop_settings(session)
    resolved: list[ShopOrder] = []
    for order in stuck:
        if order.extends_account_id is not None:
            await _sweep_extension(session, order)
            resolved.append(order)
            continue
        username = f"{settings.username_prefix}{order.id}"
        created = await _find_our_marzban_user(username, order.id)
        if created is not None:
            logger.warning(
                "Shop order #%s was stuck but %s exists in Marzban — delivering it late "
                "instead of refunding", order.id, username,
            )
            try:
                _record_delivered(session, order, username, created)
            except Exception:
                session.rollback()
                logger.exception("Shop order #%s: could not record the late delivery", order.id)
                _mark_delivered_untracked(session, order, username, "recovered by the stuck-order sweep")
        else:
            logger.warning("Shop order #%s stuck since %s with no account in Marzban — refunding",
                           order.id, order.created_at)
            refund_order(
                session,
                order,
                reason="Provisioning never completed (server restarted?) — refunded automatically",
            )
        resolved.append(order)
    return resolved


# ── top-ups ───────────────────────────────────────────────────────────────


# Characters a reference code is built from. No 0/O/1/I/L: the customer reads
# this off their screen and types it into a chat to ask "what happened to my
# payment", and those four are the pairs people get wrong.
_REFERENCE_ALPHABET = "ACDEFGHJKMNPQRTUVWXYZ2345789"


def _generate_reference_code(session: Session) -> str:
    """Short, unambiguous, unique. Retried rather than trusted: at four
    characters a collision is unlikely but not impossible, and two customers
    quoting the same code would make the operator's lookup ambiguous exactly
    when someone is anxious about money."""
    for _ in range(12):
        code = "".join(secrets.choice(_REFERENCE_ALPHABET) for _ in range(4))
        exists = session.exec(select(ShopTopup).where(ShopTopup.reference_code == code)).first()
        if exists is None:
            return code
    # Fall back to something guaranteed unique rather than raising — a
    # customer's payment must never fail to register because of a code.
    return f"R{secrets.token_hex(3).upper()}"


def create_topup(
    session: Session,
    shop_user: ShopUser,
    claimed_amount: int,
    receipt_file_id: Optional[str],
    order_id: Optional[int] = None,
) -> ShopTopup:
    """Records a claimed card-to-card payment.

    `order_id` binds the payment to a plan the customer already chose. That is
    what makes the flow one motion instead of two: approving such a top-up
    credits the wallet AND delivers the plan, so the customer never has to come
    back and buy a second time. The old shape — top up, wait, return, buy —
    lost people at the "return" step, who reasonably believed that sending the
    receipt WAS the purchase.
    """
    settings = get_shop_settings(session)
    if shop_user.is_blocked:
        raise ShopError("This account can't top up. Contact support.")
    if claimed_amount < settings.min_topup:
        raise ShopError(f"The smallest top-up is {settings.min_topup:,} T.")
    if claimed_amount > settings.max_topup:
        raise ShopError(f"The largest top-up is {settings.max_topup:,} T.")

    if order_id is not None:
        order = session.get(ShopOrder, order_id)
        if order is None or order.shop_user_id != shop_user.id:
            raise ShopError("That order doesn't belong to this account.")
        if order.status != ShopOrderStatus.awaiting_payment:
            raise ShopError("That order has already been paid for.")

    topup = ShopTopup(
        shop_user_id=shop_user.id,
        claimed_amount=claimed_amount,
        receipt_file_id=receipt_file_id,
        order_id=order_id,
        reference_code=_generate_reference_code(session),
    )
    session.add(topup)
    session.commit()
    session.refresh(topup)
    return topup


async def approve_topup(
    session: Session,
    topup: ShopTopup,
    *,
    approved_amount: Optional[int] = None,
) -> tuple[ShopTopup, Optional[ShopOrder]]:
    """Credits the wallet and, if this payment was sent FOR a plan, delivers it.

    Returns (topup, delivered_order). delivered_order is None for a plain
    wallet top-up, and for an order-bound one that could not be fulfilled —
    the caller reports the difference to the customer, since "your wallet is
    charged" and "your subscription is ready" are very different messages to
    receive after sending money to a stranger.

    Refuses a top-up that isn't pending. That guard is what stops a double-tap
    on the operator's approve button crediting the same receipt twice — the
    single most likely way for this system to give money away. It also makes
    the delivery below exactly-once: the order is only paid on the one call
    that moves the top-up out of `pending`.

    Async only because of that delivery step; the money part is synchronous
    and commits before any network call is made.
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
    # Committed BEFORE the delivery attempt. The customer paid for credit;
    # that credit is theirs whether or not provisioning then works, and a
    # Marzban failure must never roll back money the operator confirmed
    # arriving in their bank account.
    session.commit()
    session.refresh(topup)

    if topup.order_id is None:
        return topup, None

    order = session.get(ShopOrder, topup.order_id)
    if order is None or order.status != ShopOrderStatus.awaiting_payment:
        # Already handled, cancelled, or gone. The credit stands; the customer
        # simply has balance rather than a delivered plan.
        return topup, None

    try:
        delivered = await pay_awaiting_order(session, order)
    except ShopError as exc:
        # Most often: the operator approved LESS than the plan costs. Not an
        # error the operator needs to fix — the money is banked, the plan is
        # still waiting, and the customer is told what's missing.
        logger.info("Order #%s stays awaiting payment after top-up #%s: %s", order.id, topup.id, exc)
        return topup, None
    return topup, delivered


async def pay_awaiting_order(session: Session, order: ShopOrder) -> ShopOrder:
    """Turns an `awaiting_payment` order into a paid, provisioned one.

    Separate from purchase() because the money arrives at a different moment:
    purchase() debits a wallet the customer already funded, this one runs when
    a payment lands against a plan chosen earlier. Both end in the same
    _provision_order, so there is exactly one path that creates a Marzban user
    for a shop order.
    """
    if order.status != ShopOrderStatus.awaiting_payment:
        raise ShopError(f"Order #{order.id} is already {order.status.value}.")

    settings = get_shop_settings(session)
    async with _lock_for(order.shop_user_id):
        balance = wallet_balance(session, order.shop_user_id)
        if balance < order.price:
            raise ShopError(
                f"Not enough balance: this plan costs {order.price:,} T "
                f"and the wallet has {balance:,} T."
            )
        order.status = ShopOrderStatus.provisioning
        session.add(order)
        post_wallet_entry(
            session,
            order.shop_user_id,
            entry_type=ShopWalletEntryType.purchase,
            amount=-order.price,
            note=f"{order.data_limit_gb:g} GB / {order.duration_days} days",
            order_id=order.id,
            commit=False,
        )
        session.commit()
        session.refresh(order)

    await _provision_order(session, order, settings)
    session.refresh(order)
    return order


async def deliver_order_to_customer(session: Session, order: ShopOrder) -> bool:
    """Sends the customer their QR, their link and how to use it.

    THE ONLY place a subscription is handed over, deliberately. Delivery is
    triggered from two very different moments — an instant purchase from a
    funded wallet, and an operator approving a card payment hours later — and
    the customer must receive exactly the same thing either way. Two copies of
    this would drift, and it is the most important message in the product.

    Best-effort by design: the account already exists and is already paid for,
    so a Telegram failure must not undo anything. Returns whether the customer
    actually got it, so the caller can tell the operator when someone needs
    their link sent by hand.
    """
    from app.bulk_accounts import resolve_subscription_url
    from app.notify import send_photo_to_shop_user, send_to_shop_user
    from app.qr import subscription_qr_png
    from app import shop_texts

    user = session.get(ShopUser, order.shop_user_id)
    if user is None:
        logger.error("Order #%s has no shop user to deliver to", order.id)
        return False

    account = session.get(Account, order.account_id) if order.account_id else None
    url = resolve_subscription_url(account.subscription_url) if account else None
    settings = get_shop_settings(session)
    is_trial = order.price == 0

    if order.extends_account_id is not None and account is not None:
        # Renewed in place: the customer already holds this link, so the
        # message is about what changed — and that they need to do nothing.
        remaining_gb = None
        days_left = None
        if account.data_limit is not None:
            remaining_gb = max(0, account.data_limit - account.used_traffic) / (1024 ** 3)
        if account.expire:
            days_left = max(0, int((account.expire - utcnow().timestamp()) // 86400))
        try:
            await send_to_shop_user(
                user.telegram_id,
                shop_texts.renewed_in_place(order.data_limit_gb, remaining_gb, days_left, settings.support_handle),
            )
            return True
        except Exception:
            logger.exception("Order #%s: renewal notice could not be sent", order.id)
            return False

    if not url:
        logger.error("Order #%s delivered but has no subscription link to send", order.id)
        try:
            await send_to_shop_user(
                user.telegram_id,
                "سرویس‌تان ساخته شد ولی لینکش آماده نشد. "
                "چند لحظه بعد از «📱 سرویس‌های من» برش دارید."
                + shop_texts.support_line(settings.support_handle),
            )
        except Exception:
            logger.exception("Order #%s: could not warn the customer about the missing link", order.id)
        return False

    delivered = False
    try:
        await send_photo_to_shop_user(
            user.telegram_id,
            subscription_qr_png(url),
            shop_texts.delivery_caption(order.data_limit_gb, order.duration_days, is_trial=is_trial),
            filename=f"{order.marzban_username or order.id}.png",
        )
        delivered = True
    except Exception:
        logger.exception("Order #%s: QR image could not be sent", order.id)

    # The link goes as its own message even when the QR failed — a customer
    # who can copy a URL is not blocked by a missing image, and this is the
    # part that actually carries the service.
    try:
        await send_to_shop_user(user.telegram_id, shop_texts.delivery_link(url))
        await send_to_shop_user(user.telegram_id, shop_texts.setup_guide(settings.support_handle))
        delivered = True
    except Exception:
        logger.exception("Order #%s: subscription link could not be sent", order.id)

    return delivered


async def grant_trial(session: Session, shop_user: ShopUser) -> ShopOrder:
    """Gives a first-time visitor a real, working subscription for free.

    WHY THIS EXISTS, since it is the only place this codebase deliberately
    gives away inventory: in this market there is no escrow, no refund, no
    app-store rating and no payment gateway. The buyer is asked to send money
    to a stranger's personal card and wait. Every competitor asks the same,
    which means nothing distinguishes an honest shop from a dishonest one at
    the moment the customer has to decide.

    A trial is the only mechanism that reverses that order: the shop goes
    first. It also moves the genuinely hard step — installing a client app and
    importing a subscription — to BEFORE any money changes hands, where
    failure costs the customer nothing instead of looking like fraud.

    Priced as one gigabyte of bandwidth. A single support conversation with a
    customer who paid and then couldn't connect costs more.

    Recorded as a normal ShopOrder with price=0 so it appears in the operator's
    order list, counts toward nothing in the wallet, and reuses the one
    provisioning path rather than inventing a second way to create an account.
    """
    settings = get_shop_settings(session)
    if not settings.trial_enabled:
        raise ShopError("The free trial isn't available right now.")
    if shop_user.is_blocked:
        raise ShopError("This account can't take a trial. Contact support.")
    if shop_user.trial_taken_at is not None:
        raise ShopError("You've already used your free trial.")
    if is_existing_customer(session, shop_user.id):
        # A trial exists to let a stranger see the service work. Someone who
        # already has one gains nothing from it but free data.
        raise ShopError("The free trial is for new customers.")

    # Written BEFORE provisioning, and committed. If Marzban then fails, the
    # customer has burned their trial and gets an error — which is the safe
    # direction. The opposite order lets a retry loop mint unlimited free
    # accounts, and this is the one endpoint that hands out something for
    # nothing, so it is the one that has to fail closed.
    shop_user.trial_taken_at = utcnow()
    session.add(shop_user)

    order = ShopOrder(
        shop_user_id=shop_user.id,
        data_limit_gb=settings.trial_gb,
        # Rounded up, and only a LABEL for the operator's order list — the
        # real expiry is set from trial_hours below, so a 6-hour trial really
        # lasts 6 hours even though this column has to say 1.
        duration_days=max(1, (settings.trial_hours + 23) // 24),
        price=0,
        status=ShopOrderStatus.provisioning,
    )
    session.add(order)
    session.commit()
    session.refresh(order)

    await _provision_order(session, order, settings, duration_hours=settings.trial_hours)
    session.refresh(order)
    return order


def create_awaiting_order(session: Session, shop_user: ShopUser, data_limit_gb: float) -> ShopOrder:
    """Records what the customer chose, before asking them for any money.

    This is the whole point of the order-first flow: the customer commits to a
    plan while it costs them nothing, and the payment request that follows
    carries a single exact number instead of asking them to invent one and do
    the multiplication themselves.

    Takes no money and holds nothing, so an abandoned order is free. They are
    not cleaned up on a timer for that reason — an old awaiting_payment row is
    a record of what someone was interested in, not a leak.
    """
    if shop_user.is_blocked:
        raise ShopError("This account can't make purchases. Contact support.")
    settings = get_shop_settings(session)
    validate_purchase_request(settings, data_limit_gb)
    price = quote_price(settings, data_limit_gb)

    order = ShopOrder(
        shop_user_id=shop_user.id,
        data_limit_gb=data_limit_gb,
        duration_days=settings.plan_duration_days,
        price=price,
        status=ShopOrderStatus.awaiting_payment,
    )
    session.add(order)
    session.commit()
    session.refresh(order)
    return order


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


# ── renewal: warn before the service stops, not after ─────────────────────
#
# The previous flow had no renewal path at all: when a month ended the VPN
# simply stopped, and the customer's first news of it was a connection that
# no longer worked. For a business selling monthly subscriptions that is the
# single largest revenue leak there is — the customer who was satisfied
# enough to renew is lost at exactly the moment they would have paid again.
#
# The panel-tracking layer already knows every account's expiry and usage
# (the sync job refreshes them every minute). This points that knowledge at
# the customer.

# How far ahead a paid plan's expiry is announced. Three days: enough time to
# arrange a card transfer and have it approved, not so early that the message
# is forgotten by the time it matters.
EXPIRY_WARN_DAYS = 3
# Share of the data allowance used before a usage warning. 80% leaves room for
# a renewal to land before the connection actually stops.
USAGE_WARN_PERCENT = 80
# A trial is measured in hours, so its warning is too.
TRIAL_WARN_HOURS = 2


async def warn_customers_before_service_ends(session: Session) -> int:
    """Sends each delivered order at most one expiry warning and one usage
    warning, ever. Returns how many were sent.

    Only a warning that was actually DELIVERED is recorded — a Telegram
    failure leaves the order unmarked so the next pass tries again, instead
    of the customer silently never hearing.
    """
    from app import shop_texts
    from app.notify import send_to_shop_user

    settings = get_shop_settings(session)
    handle = settings.support_handle
    now_ts = utcnow().timestamp()
    sent = 0

    orders = session.exec(
        select(ShopOrder).where(
            ShopOrder.status == ShopOrderStatus.delivered,
            ShopOrder.account_id.is_not(None),
        )
    ).all()
    # One account can carry several orders now that renewals extend in place.
    # Only the newest speaks for it — otherwise every older order on the same
    # account would fire its own warning about the same service.
    latest_by_account: dict[int, ShopOrder] = {}
    for candidate in orders:
        held = latest_by_account.get(candidate.account_id)
        if held is None or candidate.id > held.id:
            latest_by_account[candidate.account_id] = candidate
    for order in latest_by_account.values():
        account = session.get(Account, order.account_id)
        user = session.get(ShopUser, order.shop_user_id)
        if account is None or user is None or user.is_blocked:
            continue
        # An account the operator disabled or that Marzban no longer has is
        # not the customer's to renew; warning them about it would only
        # generate a confused support message.
        if account.status in ("disabled", "deleted_from_marzban"):
            continue
        is_trial = order.price == 0

        message = None
        mark = None
        if order.expiry_warned_at is None and account.expire:
            seconds_left = account.expire - now_ts
            if is_trial:
                if 0 < seconds_left <= TRIAL_WARN_HOURS * 3600:
                    message = shop_texts.trial_ending(max(1, int(seconds_left // 3600)), handle)
                    mark = "expiry"
            elif 0 < seconds_left <= EXPIRY_WARN_DAYS * 86400:
                days_left = max(1, int(-(-seconds_left // 86400)))
                message = shop_texts.expiring_soon(order.data_limit_gb, days_left, handle)
                mark = "expiry"

        if message is None and order.usage_warned_at is None and not is_trial and account.data_limit:
            percent = int(account.used_traffic * 100 / account.data_limit)
            if USAGE_WARN_PERCENT <= percent < 100:
                message = shop_texts.data_almost_gone(order.data_limit_gb, percent, handle)
                mark = "usage"

        if message is None:
            continue
        try:
            await send_to_shop_user(user.telegram_id, message)
        except Exception:
            logger.exception("Could not send the %s warning for order #%s", mark, order.id)
            continue
        if mark == "expiry":
            order.expiry_warned_at = utcnow()
        else:
            order.usage_warned_at = utcnow()
        session.add(order)
        session.commit()
        sent += 1
    return sent


# ── renewal IN PLACE ──────────────────────────────────────────────────────
#
# A customer who already has a working account gets their new plan ADDED to
# that account: same Marzban user, same subscription link. Their VPN app
# refreshes the subscription on its own and nothing needs re-importing.
#
# The alternative this replaced — a brand-new account per purchase — was
# found independently by two review passes to be the flow's largest remaining
# leak: the trial's "buy so you don't get cut off" was false because the trial
# link died regardless, and every later month meant importing a new link and
# living with a dead duplicate in the app.
#
# Money rules are the same as for a new account and exist for the same
# reasons: the plan is paid for before Marzban is called, a failed call is
# NOT evidence that nothing changed, and nothing is refunded without first
# asking the panel. What differs is the evidence: for a create it is "does
# the user exist with our note", for an extension it is "has the account's
# limit and expiry already reached the targets we recorded".

# Serialises read-modify-write on ONE account. Two purchases landing together
# would both read the same current limit and both write base+plan, silently
# dropping one plan's gigabytes. Same single-process caveat as _purchase_locks.
_extend_locks: dict[int, asyncio.Lock] = {}


def _extend_lock_for(account_id: int) -> asyncio.Lock:
    lock = _extend_locks.get(account_id)
    if lock is None:
        lock = asyncio.Lock()
        _extend_locks[account_id] = lock
    return lock


def renewable_account(session: Session, shop_user_id: int) -> Optional[Account]:
    """The account a new purchase should extend, or None to create one.

    The customer's most recently delivered account that still exists and that
    the operator has not disabled. Trials count — upgrading the trial in place
    is the whole point: the link they tested with is the link they keep.
    """
    orders = session.exec(
        select(ShopOrder)
        .where(
            ShopOrder.shop_user_id == shop_user_id,
            ShopOrder.status == ShopOrderStatus.delivered,
            ShopOrder.account_id.is_not(None),
        )
        .order_by(ShopOrder.id.desc())
    ).all()
    for candidate in orders:
        account = session.get(Account, candidate.account_id)
        if account is None:
            continue
        if account.status in ("disabled", "deleted_from_marzban"):
            continue
        return account
    return None


def is_existing_customer(session: Session, shop_user_id: int) -> bool:
    """Has this person ever had a service from us? Used to keep the free
    trial for strangers: an existing customer taking a 'trial' would just be
    free data on top of what they already pay for."""
    return session.exec(
        select(ShopOrder).where(
            ShopOrder.shop_user_id == shop_user_id,
            ShopOrder.status == ShopOrderStatus.delivered,
        )
    ).first() is not None


async def _extension_landed(username: str, target_limit: int, target_expire: int) -> Optional[dict]:
    """The panel's user if the extension is already applied, else None.

    'Applied' means the limit and expiry have REACHED the targets, not that
    they changed at all — an unrelated edit must not be mistaken for our
    modify having landed. A lookup that itself fails returns None and sends
    the caller to the refund path: a refunded customer whose extension did
    land is recoverable by the operator; charging for one that didn't is not.
    """
    try:
        user = await marzban_client.get_user(username)
    except Exception:
        logger.exception("Could not check whether the extension of %s landed", username)
        return None
    if user is None:
        return None
    if (user.get("data_limit") or 0) >= target_limit and (user.get("expire") or 0) >= target_expire - 60:
        return user
    return None


def _record_extended(session: Session, order: ShopOrder, account: Account, marzban_user: dict) -> None:
    """Mirrors the extended account locally and marks the order delivered.

    Same guard as _record_delivered: if the sweeper settled this order in its
    own session while Marzban was working, re-charge rather than let a paid
    extension become a free one.
    """
    session.expire(order)
    session.refresh(order)
    if order.status != ShopOrderStatus.provisioning:
        logger.error(
            "Order #%s reached extension as '%s', not 'provisioning' — re-charging so the "
            "added volume is not free.", order.id, order.status.value,
        )
        post_wallet_entry(
            session,
            order.shop_user_id,
            entry_type=ShopWalletEntryType.purchase,
            amount=-order.price,
            note=f"Re-charge: order #{order.id} was refunded but extended anyway",
            order_id=order.id,
            commit=False,
        )
    now = utcnow()
    sync_marzban_fields(account, marzban_user)
    account.last_synced_at = now
    session.add(account)
    session.add(AccountEvent(
        account_id=account.id,
        action="extend",
        detail=(f"Renewed in place via shop order #{order.id} "
                f"(+{order.data_limit_gb:g} GB, +{order.duration_days} days)"),
    ))
    order.account_id = account.id
    order.marzban_username = account.marzban_username
    order.status = ShopOrderStatus.delivered
    order.delivered_at = now
    order.error = None
    session.add(order)
    session.commit()


async def _extend_order(session: Session, order: ShopOrder, account: Account) -> bool:
    """Adds this order's plan to `account`. Returns False only when the
    account turned out not to be extendable (gone from the panel) and the
    caller should create a new one instead; True means the order reached a
    terminal state here — delivered or refunded. Never raises.

    Stacks rather than resets. Remaining gigabytes and remaining days are
    things the customer already paid for; resetting on renewal would quietly
    confiscate them, and renewing EARLY is exactly the behaviour the expiry
    warnings exist to encourage.
    """
    username = account.marzban_username
    async with _extend_lock_for(account.id):
        try:
            current = await marzban_client.get_user(username)
        except Exception as exc:  # noqa: BLE001 — nothing has been changed yet, so refunding is safe
            logger.exception("Order #%s: could not read %s to extend it", order.id, username)
            refund_order(session, order, reason=f"Could not read the account to extend: {exc}")
            return True
        if current is None:
            logger.warning("Order #%s: %s is gone from the panel — creating a new account instead",
                           order.id, username)
            return False

        used = int(current.get("used_traffic") or 0)
        limit = current.get("data_limit")
        base_limit = int(limit) if limit is not None else used
        target_limit = base_limit + bytes_from_gb(order.data_limit_gb)
        now_ts = int(utcnow().timestamp())
        target_expire = max(now_ts, int(current.get("expire") or 0)) + order.duration_days * SECONDS_IN_DAY

        # Committed BEFORE the call: these are the evidence a timeout or a
        # crash is later checked against. See ShopOrder.target_data_limit.
        order.extends_account_id = account.id
        order.target_data_limit = target_limit
        order.target_expire = target_expire
        session.add(order)
        session.commit()

        try:
            updated = await marzban_client.modify_user(
                username, {"data_limit": target_limit, "expire": target_expire, "status": "active"},
            )
        except (ValueError, MarzbanUnavailable, MarzbanAuthError) as exc:
            logger.exception("Order #%s: modify of %s failed or timed out", order.id, username)
            landed = await _extension_landed(username, target_limit, target_expire)
            if landed is not None:
                logger.warning("Order #%s: the modify call failed but the extension is on the "
                               "panel — delivering instead of refunding", order.id)
                try:
                    _record_extended(session, order, account, landed)
                except Exception:
                    session.rollback()
                    logger.exception("Order #%s: extension landed but could not be recorded", order.id)
                    _mark_delivered_untracked(session, order, username, "extension adopted after a failed call")
                return True
            refund_order(session, order, reason=str(exc))
            return True

        try:
            _record_extended(session, order, account, updated)
        except Exception as exc:  # noqa: BLE001 — the panel is already extended; never drop it silently
            session.rollback()
            logger.exception("Order #%s: extended %s but failed to record it", order.id, username)
            _mark_delivered_untracked(session, order, username, str(exc))
        return True


# ── keeping the promise about the wait ────────────────────────────────────


async def notify_overdue_payments(session: Session) -> int:
    """Tells a customer, once, when their payment has waited past the promised
    time — and tells the operator in the same breath.

    Silence after a stated deadline is the single most likely moment for an
    honest shop to be taken for a scam: the customer has sent money to a
    personal card, was told "usually within N minutes", and N minutes have
    passed with nothing. A sentence that owns the delay costs nothing and
    turns a broken promise into evidence that someone is actually there.
    """
    from app import shop_texts
    from app.notify import notify_admin, send_to_shop_user

    settings = get_shop_settings(session)
    cutoff = utcnow() - timedelta(minutes=settings.approval_eta_minutes)
    overdue = session.exec(
        select(ShopTopup).where(
            ShopTopup.status == ShopTopupStatus.pending,
            ShopTopup.overdue_notified_at.is_(None),
            ShopTopup.receipt_file_id.is_not(None),
            ShopTopup.created_at < cutoff,
        )
    ).all()
    sent = 0
    for topup in overdue:
        user = session.get(ShopUser, topup.shop_user_id)
        if user is None:
            continue
        try:
            await send_to_shop_user(
                user.telegram_id,
                shop_texts.payment_overdue(topup.reference_code, settings.support_handle),
            )
        except Exception:
            logger.exception("Could not tell the customer that payment #%s is late", topup.id)
            continue
        topup.overdue_notified_at = utcnow()
        session.add(topup)
        session.commit()
        sent += 1
        try:
            await notify_admin(
                f"⏰ Payment #{topup.id} ({topup.reference_code or 'no code'}) has waited past your "
                f"{settings.approval_eta_minutes}-minute promise. The customer has been told it is late."
            )
        except Exception:
            logger.exception("Could not alert the operator that payment #%s is late", topup.id)
    return sent


# ── picking a conversation back up ────────────────────────────────────────


def latest_awaiting_order(session: Session, shop_user_id: int, max_age_hours: int = 48) -> Optional[ShopOrder]:
    """The order a receipt most likely belongs to, when the bot has lost track.

    The bot keeps "what did this person tap last" in memory. That is lost on a
    restart — and in Iran it is routinely lost for a more ordinary reason:
    banking apps refuse to open over a VPN, so the customer turns the VPN off
    to pay, Telegram drops with it, and the receipt arrives in what the bot
    sees as a brand-new conversation. Recovering the order from the database
    means the receipt still lands on the right plan.
    """
    cutoff = utcnow() - timedelta(hours=max_age_hours)
    return session.exec(
        select(ShopOrder)
        .where(
            ShopOrder.shop_user_id == shop_user_id,
            ShopOrder.status == ShopOrderStatus.awaiting_payment,
            ShopOrder.created_at > cutoff,
        )
        .order_by(ShopOrder.id.desc())
    ).first()


def find_topup_by_code(session: Session, shop_user_id: int, code: str) -> Optional[ShopTopup]:
    """A customer's own payment by the code they were given. Scoped to them:
    a code is short, and one customer must never be able to read another's
    payment by guessing."""
    return session.exec(
        select(ShopTopup).where(
            ShopTopup.shop_user_id == shop_user_id,
            ShopTopup.reference_code == code,
        )
    ).first()


async def _sweep_extension(session: Session, order: ShopOrder) -> None:
    """The stuck-order sweeper's branch for renewals. Delivers if the panel
    already shows the extension, refunds otherwise — never re-applies it,
    since a second modify on top of a landed one would give the customer the
    plan twice."""
    account = session.get(Account, order.extends_account_id)
    if account is not None and order.target_data_limit is not None and order.target_expire is not None:
        landed = await _extension_landed(account.marzban_username, order.target_data_limit, order.target_expire)
        if landed is not None:
            try:
                _record_extended(session, order, account, landed)
            except Exception:
                session.rollback()
                logger.exception("Order #%s: could not record the recovered extension", order.id)
                _mark_delivered_untracked(session, order, account.marzban_username,
                                          "extension recovered by the stuck-order sweep")
            return
    refund_order(session, order, reason="Renewal never completed (server restarted?) — refunded automatically")
