"""Business logic for delegated self-service (see models.py's Delegate
docstring for the trust-boundary reasoning).

Every function here that touches an account takes the Delegate and re-checks
ownership itself — never trusts an account_id alone. A Delegate scoped to
customer 7 must never be able to renew or delete account 42 just because
they know its id; every entry point re-derives "is this actually theirs"
from the Delegate row, the same way wallet.py never trusts a display name
for the action that actually moves money.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from sqlmodel import Session, select

from app.config import settings as app_settings
from app.marzban_client import MarzbanAuthError, MarzbanUnavailable, marzban_client
from app.models import (
    Account,
    AccountEvent,
    Customer,
    Delegate,
    Group,
    LedgerEntry,
    LedgerSource,
    LedgerType,
    utcnow,
)
from app.notify import notify_admin
from app.services import MoneyBook, bytes_from_gb, effective_rate

logger = logging.getLogger(__name__)

SECONDS_IN_DAY = 86400


class DelegateError(Exception):
    """Something the DELEGATE should be told, in their own words — over the
    credit limit, over the daily cap, not their account. Distinct from an
    unexpected exception, which the bot reports as a generic failure and the
    operator finds in the logs."""


def get_active_delegate(session: Session, telegram_id: int) -> Optional[Delegate]:
    delegate = session.exec(select(Delegate).where(Delegate.telegram_id == telegram_id)).first()
    if delegate is None or not delegate.is_active:
        return None
    return delegate


def scope_name(session: Session, delegate: Delegate) -> str:
    if delegate.customer_id is not None:
        customer = session.get(Customer, delegate.customer_id)
        return customer.name if customer else "?"
    if delegate.group_id is not None:
        group = session.get(Group, delegate.group_id)
        return group.name if group else "?"
    return "?"


def _scope_filter(delegate: Delegate):
    """Exactly one of customer_id/group_id is set on a Delegate — see the
    model docstring. A customer delegate reaches only accounts owned
    directly by that customer (not routed through a group, same "grouped
    accounts roll up through their group, never also their own customer"
    rule MoneyBook uses); a group delegate reaches every member account."""
    if delegate.group_id is not None:
        return Account.group_id == delegate.group_id
    return (Account.customer_id == delegate.customer_id) & (Account.group_id.is_(None))


def list_delegate_accounts(session: Session, delegate: Delegate) -> list[Account]:
    stmt = select(Account).where(_scope_filter(delegate), Account.deleted_at.is_(None))
    return session.exec(stmt).all()


def _get_owned_account(session: Session, delegate: Delegate, account_id: int) -> Account:
    """Raises DelegateError rather than returning None — every caller needs
    the same "not yours" message, and a 404-shaped None would tempt a caller
    to write its own (weaker) check instead of relying on this one."""
    account = session.exec(
        select(Account).where(Account.id == account_id, _scope_filter(delegate), Account.deleted_at.is_(None))
    ).first()
    if account is None:
        raise DelegateError("این اکانت متعلق به شما نیست یا حذف شده.")
    return account


def _posted_debt(session: Session, delegate: Delegate) -> float:
    book = MoneyBook(session)
    if delegate.group_id is not None:
        group = session.get(Group, delegate.group_id)
        return book.group_posted(group) if group else 0.0
    customer = session.get(Customer, delegate.customer_id)
    return book.customer_posted(customer) if customer else 0.0


def _check_credit_limit(session: Session, delegate: Delegate) -> None:
    if delegate.credit_limit is None:
        return
    posted = _posted_debt(session, delegate)
    if posted >= delegate.credit_limit:
        raise DelegateError(
            f"سقف بدهی شما پر شده ({posted:,.0f} از {delegate.credit_limit:,.0f}). "
            "تا تسویه با تیم فروش، امکان ساخت/تمدید اکانت جدید نیست."
        )


def _check_daily_cap(session: Session, delegate: Delegate) -> None:
    """A raw COUNT guard, not a money guard — see the Delegate model's own
    daily_create_cap docstring for why this exists alongside, not instead
    of, the credit limit above."""
    since = utcnow() - timedelta(days=1)
    stmt = select(Account).where(_scope_filter(delegate), Account.created_at >= since)
    count = len(session.exec(stmt).all())
    if count >= delegate.daily_create_cap:
        raise DelegateError(
            f"امروز به سقف ساخت اکانت رسیدید ({delegate.daily_create_cap} تا). فردا دوباره امتحان کنید."
        )


async def _collect_taken_usernames(session: Session) -> set[str]:
    """Same reasoning as bulk_accounts' own version (routers/accounts.py):
    a local-only check would happily plan a name Marzban already has."""
    local = {row for row in session.exec(select(Account.marzban_username)).all() if row}
    try:
        marzban_users = await marzban_client.list_all_users()
    except (MarzbanUnavailable, MarzbanAuthError) as exc:
        raise DelegateError("سرور موقتاً در دسترس نیست، چند دقیقه دیگر دوباره امتحان کنید.") from exc
    remote = {u.get("username") for u in marzban_users if u.get("username")}
    return local | remote


async def _next_username(session: Session, delegate: Delegate) -> str:
    taken = await _collect_taken_usernames(session)
    n = 1
    while f"{delegate.username_prefix}{n}" in taken:
        n += 1
    return f"{delegate.username_prefix}{n}"


async def _notify_delegate_action(session: Session, delegate: Delegate, text: str) -> None:
    """Best-effort, deliberately: this fires AFTER the action already
    committed (create/renew/delete already happened), so a failed Telegram
    send must not roll anything back or block the delegate's own reply —
    same reasoning notify.py documents for any post-hoc notification."""
    try:
        await notify_admin(f"👤 دلگیت «{scope_name(session, delegate)}»: {text}")
    except Exception:
        logger.exception("Could not notify operator of a delegate action (delegate_id=%s)", delegate.id)


async def create_delegate_account(session: Session, delegate: Delegate, data_limit_gb: float) -> Account:
    if not delegate.is_active:
        raise DelegateError("دسترسی شما غیرفعال شده — با تیم فروش تماس بگیرید.")
    _check_credit_limit(session, delegate)
    _check_daily_cap(session, delegate)

    username = await _next_username(session, delegate)
    now = utcnow()
    expire = int(now.timestamp()) + delegate.default_duration_days * SECONDS_IN_DAY
    data_limit = bytes_from_gb(data_limit_gb)

    marzban_payload = {
        "username": username,
        "proxies": app_settings.marzban_default_proxies,
        "inbounds": app_settings.marzban_default_inbounds,
        "expire": expire,
        "data_limit": data_limit,
        "data_limit_reset_strategy": "no_reset",
        "status": "active",
        "note": f"Created via delegate self-service ({scope_name(session, delegate)})",
    }

    try:
        marzban_user = await marzban_client.create_user(marzban_payload)
    except ValueError as exc:
        raise DelegateError(f"ساخت اکانت رد شد: {exc}") from exc
    except (MarzbanUnavailable, MarzbanAuthError) as exc:
        raise DelegateError("سرور موقتاً در دسترس نیست، چند دقیقه دیگر دوباره امتحان کنید.") from exc

    account = Account(
        marzban_username=username,
        customer_id=delegate.customer_id,
        group_id=delegate.group_id,
        used_traffic=marzban_user.get("used_traffic", 0),
        lifetime_used_traffic=marzban_user.get("lifetime_used_traffic", 0),
        first_seen_traffic=marzban_user.get("lifetime_used_traffic", 0),
        first_seen_traffic_at=now,
        usage_baseline_at=now,
        data_limit=marzban_user.get("data_limit"),
        expire=marzban_user.get("expire"),
        status=marzban_user.get("status"),
        last_synced_at=now,
        # billed_data_limit set to the full package below, in the same
        # transaction as the charge — this account is charged for its whole
        # package the moment it's created (see effective_rate below), so
        # nothing about it should also read as "pending" on the dashboard.
        billed_data_limit=data_limit or 0,
    )
    session.add(account)
    session.flush()  # need account.id for the ledger entry + event below

    rate = effective_rate(session, account)
    amount = round(data_limit_gb * rate, 2)
    if amount > 0:
        session.add(LedgerEntry(
            type=LedgerType.charge,
            amount=amount,
            customer_id=delegate.customer_id,
            account_id=account.id,
            note=f"Delegate self-service: created {data_limit_gb:g}GB / {delegate.default_duration_days}d",
            source=LedgerSource.delegate,
        ))
    session.add(AccountEvent(
        account_id=account.id,
        action="delegate_create",
        detail=f"data_limit_gb={data_limit_gb:g}, duration_days={delegate.default_duration_days}, delegate_id={delegate.id}",
        source=LedgerSource.delegate,
    ))
    session.commit()
    session.refresh(account)

    await _notify_delegate_action(
        session, delegate,
        f"اکانت جدید ساخت: {username} ({data_limit_gb:g}GB/{delegate.default_duration_days}روزه) — بدهی {amount:,.0f} ثبت شد."
    )
    return account


async def renew_delegate_account(
    session: Session, delegate: Delegate, account_id: int, extend_gb: float, extend_days: Optional[int],
) -> Account:
    if not delegate.is_active:
        raise DelegateError("دسترسی شما غیرفعال شده — با تیم فروش تماس بگیرید.")
    _check_credit_limit(session, delegate)
    account = _get_owned_account(session, delegate, account_id)

    days = extend_days if extend_days is not None else delegate.default_duration_days
    base_expire = account.expire if account.expire else int(utcnow().timestamp())
    new_expire = base_expire + days * SECONDS_IN_DAY
    base_data_limit = account.data_limit or 0
    new_data_limit = base_data_limit + bytes_from_gb(extend_gb)

    try:
        marzban_user = await marzban_client.modify_user(account.marzban_username, {
            "expire": new_expire,
            "data_limit": new_data_limit,
        })
    except ValueError as exc:
        raise DelegateError(f"تمدید رد شد: {exc}") from exc
    except (MarzbanUnavailable, MarzbanAuthError) as exc:
        raise DelegateError("سرور موقتاً در دسترس نیست، چند دقیقه دیگر دوباره امتحان کنید.") from exc

    account.expire = marzban_user.get("expire", new_expire)
    account.data_limit = marzban_user.get("data_limit", new_data_limit)
    account.status = marzban_user.get("status", account.status)
    account.billed_data_limit = (account.billed_data_limit or 0) + bytes_from_gb(extend_gb)
    account.last_synced_at = utcnow()
    session.add(account)

    rate = effective_rate(session, account)
    amount = round(extend_gb * rate, 2)
    if amount > 0:
        session.add(LedgerEntry(
            type=LedgerType.charge,
            amount=amount,
            customer_id=delegate.customer_id,
            account_id=account.id,
            note=f"Delegate self-service: renewed +{extend_gb:g}GB / +{days}d",
            source=LedgerSource.delegate,
        ))
    session.add(AccountEvent(
        account_id=account.id,
        action="delegate_renew",
        detail=f"extend_gb={extend_gb:g}, extend_days={days}, delegate_id={delegate.id}",
        source=LedgerSource.delegate,
    ))
    session.commit()
    session.refresh(account)

    await _notify_delegate_action(
        session, delegate,
        f"تمدید کرد: {account.marzban_username} (+{extend_gb:g}GB/+{days}روز) — بدهی {amount:,.0f} ثبت شد."
    )
    return account


async def delete_delegate_account(session: Session, delegate: Delegate, account_id: int) -> None:
    if not delegate.is_active:
        raise DelegateError("دسترسی شما غیرفعال شده — با تیم فروش تماس بگیرید.")
    account = _get_owned_account(session, delegate, account_id)

    try:
        await marzban_client.delete_user(account.marzban_username)
    except ValueError as exc:
        raise DelegateError(f"حذف رد شد: {exc}") from exc
    except (MarzbanUnavailable, MarzbanAuthError) as exc:
        raise DelegateError("سرور موقتاً در دسترس نیست، چند دقیقه دیگر دوباره امتحان کنید.") from exc

    account.deleted_at = utcnow()
    session.add(account)
    session.add(AccountEvent(
        account_id=account.id,
        action="delegate_delete",
        detail=f"delegate_id={delegate.id}",
        source=LedgerSource.delegate,
    ))
    session.commit()

    await _notify_delegate_action(
        session, delegate,
        f"حذف کرد: {account.marzban_username} — اگر لازم است دستی اعتبار برگردانید."
    )
