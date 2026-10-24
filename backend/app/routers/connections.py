"""Who is connected to this operation through Telegram — and what they did
there (2026-10-24, the admin-UX rebuild).

Two operator questions this answers that no existing endpoint put side by side:

1. «کدوم کاربر/خانواده از طریق تلگرام وصله؟» — per customer: the shop-bot link
   (claimed invite → their owner pressed Start in the shop bot and manages
   their own services from there; pending invite → a link is minted but
   unclaimed), the delegate grant (a separate trust boundary with its own
   bot), and the raw counts. Everything the DelegatesPage and the per-customer
   ShopLinkCard show, in ONE table over EVERY customer.

2. «تاریخچه‌ی هر کاربر با بات» — a merged timeline of everything that happened
   through the bots for one customer: shop orders/top-ups/wallet movements,
   delegate self-service account events, and operator-bot ledger/events. Built
   ENTIRELY from rows other flows already write (ShopOrder, ShopTopup,
   ShopWalletEntry, AccountEvent, LedgerEntry) — no bot changes, no new
   logging; the bots already leave every trace this needs.

Read-only, operator-only (same JWT gate as every other panel router).
"""

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.auth import require_auth
from app.db import get_session
from app.models import (
    Account,
    AccountEvent,
    Customer,
    Delegate,
    LedgerEntry,
    LedgerSource,
    LedgerType,
    ShopBindClaim,
    ShopOrder,
    ShopTopup,
    ShopUser,
    ShopWalletEntry,
)
from app.schemas import BotHistoryItem, ConnectionRow, DelegateSummary, ShopLinkSummary
from app.routers.shop import _shop_invite_url

router = APIRouter(prefix="/api/connections", tags=["connections"], dependencies=[Depends(require_auth)])


def _shop_summary(session: Session, customer: Customer) -> ShopLinkSummary:
    """The same three states GET /api/shop/link/{id} reports, per row, for the
    whole customer list at once — plus the pending bind-by-identity claim
    (the operator typed a @username/name; the customer's first bot message
    completes it)."""
    claim = session.exec(
        select(ShopBindClaim).where(ShopBindClaim.customer_id == customer.id, ShopBindClaim.claimed_at.is_(None))
    ).first()
    claim_kwargs = {
        "bind_claim_username": claim.telegram_username if claim else None,
        "bind_claim_display_name": claim.display_name if claim else None,
        "bind_claim_created_at": claim.created_at if claim else None,
    }
    if customer.shop_user_id is not None:
        user = session.get(ShopUser, customer.shop_user_id)
        if user is not None:
            from app.shop_service import wallet_balance

            return ShopLinkSummary(
                state="linked",
                linked_at=customer.shop_link_linked_at,
                shop_user_id=user.id,
                telegram_id=user.telegram_id,
                telegram_username=user.telegram_username,
                display_name=user.display_name,
                wallet_balance=wallet_balance(session, user.id),
                last_seen_at=user.last_seen_at,
                is_blocked=user.is_blocked,
                **claim_kwargs,
            )
    if customer.shop_link_token is not None:
        return ShopLinkSummary(
            state="pending",
            invite_url=_shop_invite_url(customer.shop_link_token),
            claim_expires_at=customer.shop_link_expires_at,
            **claim_kwargs,
        )
    return ShopLinkSummary(state="none", **claim_kwargs)


def _delegate_summary(delegates: list[Delegate]) -> Optional[DelegateSummary]:
    """The customer-scoped delegate grant, collapsed to the one row that
    matters: an active claim wins, then a pending invite, then the most recent
    revoked row (kept visible so "why can't he renew any more" has an answer)."""
    if not delegates:
        return None
    claimed = [d for d in delegates if d.telegram_id is not None and d.is_active]
    if claimed:
        d = claimed[0]
        state = "claimed"
    else:
        pending = [d for d in delegates if d.telegram_id is None and d.claim_token is not None]
        if pending:
            d = pending[0]
            state = "pending"
        else:
            d = delegates[0]
            state = "revoked"
    return DelegateSummary(
        id=d.id,
        state=state,
        telegram_id=d.telegram_id,
        label=d.label,
        credit_limit=d.credit_limit,
        claim_expires_at=d.claim_expires_at,
        is_active=d.is_active,
    )


@router.get("", response_model=list[ConnectionRow])
def list_connections(session: Session = Depends(get_session)):
    customers = session.exec(select(Customer).order_by(Customer.name)).all()
    accounts = session.exec(select(Account).where(Account.deleted_at.is_(None))).all()
    account_count: dict[int, int] = {}
    for a in accounts:
        if a.customer_id is not None:
            account_count[a.customer_id] = account_count.get(a.customer_id, 0) + 1

    all_delegates = session.exec(select(Delegate).where(Delegate.customer_id.is_not(None))).all()
    delegates_by_customer: dict[int, list[Delegate]] = {}
    for d in all_delegates:
        if d.customer_id is not None:
            delegates_by_customer.setdefault(d.customer_id, []).append(d)

    rows = []
    for c in customers:
        rows.append(
            ConnectionRow(
                customer_id=c.id,
                name=c.name,
                kind=c.kind,
                contact=c.contact,
                accounts_count=account_count.get(c.id, 0),
                shop=_shop_summary(session, c),
                delegate=_delegate_summary(delegates_by_customer.get(c.id, [])),
            )
        )
    return rows


@router.get("/{customer_id}/bot-history", response_model=list[BotHistoryItem])
def customer_bot_history(customer_id: int, limit: int = 300, session: Session = Depends(get_session)):
    """Everything the bots did for this customer, newest first, merged from
    the traces those flows already wrote. `limit` caps the merged list after
    sorting (the operator reads a page, not a warehouse)."""
    customer = session.get(Customer, customer_id)
    if not customer:
        raise HTTPException(404, "Customer not found")

    items: list[BotHistoryItem] = []

    # ── the shop side ────────────────────────────────────────────────────
    if customer.shop_user_id is not None:
        user = session.get(ShopUser, customer.shop_user_id)
        if user is not None:
            username = user.display_name or user.telegram_username or f"tg {user.telegram_id}"
            for order in session.exec(
                select(ShopOrder).where(ShopOrder.shop_user_id == user.id).order_by(ShopOrder.created_at)
            ).all():
                title = {
                    "awaiting_payment": "سفارش ثبت شد (منتظر پرداخت)",
                    "provisioning": "سفارش پرداخت شد (در حال ساخت)",
                    "delivered": "سفارش تحویل شد",
                    "failed": "سفارش شکست خورد (پول برگشت)",
                }.get(order.status.value if hasattr(order.status, "value") else str(order.status), "سفارش")
                detail = f"{order.data_limit_gb:g} گیگ / {order.duration_days} روز"
                if order.service_name:
                    detail = f"«{order.service_name}» — {detail}"
                if order.marzban_username:
                    detail += f" · {order.marzban_username}"
                if order.error:
                    detail += f" · خطا: {order.error}"
                items.append(BotHistoryItem(
                    ts=order.created_at,
                    kind="order",
                    title=title,
                    detail=detail,
                    amount=float(order.price) if order.price else None,
                ))
            for topup in session.exec(
                select(ShopTopup).where(ShopTopup.shop_user_id == user.id).order_by(ShopTopup.created_at)
            ).all():
                status = topup.status.value if hasattr(topup.status, "value") else str(topup.status)
                if status == "pending":
                    title, amount = "رسید ارسال شد (منتظر تایید)", float(topup.claimed_amount)
                elif status == "approved":
                    title, amount = "واریز تایید شد", float(topup.approved_amount or topup.claimed_amount)
                else:
                    title, amount = "رسید رد شد", None
                detail = f"کد پیگیری {topup.reference_code}" if topup.reference_code else None
                if topup.reject_reason:
                    detail = (detail + " — " if detail else "") + f"دلیل: {topup.reject_reason}"
                items.append(BotHistoryItem(
                    ts=topup.created_at,
                    kind="topup",
                    title=title,
                    detail=detail,
                    amount=amount,
                ))
            for entry in session.exec(
                select(ShopWalletEntry).where(ShopWalletEntry.shop_user_id == user.id).order_by(ShopWalletEntry.created_at)
            ).all():
                etype = entry.type.value if hasattr(entry.type, "value") else str(entry.type)
                title = {
                    "topup": "شارژ کیف پول",
                    "purchase": "خرید (کسر از کیف پول)",
                    "refund": "برگشت وجه به کیف پول",
                    "adjust": "اصلاح دستی کیف پول",
                }.get(etype, etype)
                items.append(BotHistoryItem(
                    ts=entry.created_at,
                    kind="wallet",
                    title=title,
                    detail=entry.note,
                    amount=float(entry.amount),
                ))

    # ── the delegate + operator-bot side (traces on this customer's accounts) ──
    accounts = session.exec(
        select(Account).where(Account.customer_id == customer.id)
    ).all()
    # Customer-owned accounts PLUS the group members this customer represents
    # (a delegate scoped to the customer manages the direct accounts; a group
    # delegate's events ride the group's member accounts — but this timeline
    # is per-customer, so member accounts of represented groups count too).
    from app.models import Group

    represented = session.exec(
        select(Group).where(Group.representative_customer_id == customer.id)
    ).all()
    if represented:
        accounts += session.exec(
            select(Account).where(Account.group_id.in_([g.id for g in represented]))
        ).all()
    username_by_id = {a.id: a.marzban_username for a in accounts if a.id is not None}
    account_ids = list(username_by_id.keys())

    if account_ids:
        for ev in session.exec(
            select(AccountEvent).where(
                AccountEvent.account_id.in_(account_ids),  # type: ignore[arg-type]
                AccountEvent.source.in_([LedgerSource.bot, LedgerSource.delegate]),  # type: ignore[arg-type]
            )
            .order_by(AccountEvent.date)
        ).all():
            items.append(BotHistoryItem(
                ts=ev.date,
                kind="account_event",
                title=_event_title(ev),
                detail=ev.detail,
                account_username=username_by_id.get(ev.account_id),
            ))
        for entry in session.exec(
            select(LedgerEntry).where(
                LedgerEntry.account_id.in_(account_ids),  # type: ignore[arg-type]
                LedgerEntry.source.in_([LedgerSource.bot, LedgerSource.delegate]),  # type: ignore[arg-type]
            )
            .order_by(LedgerEntry.date)
        ).all():
            if entry.voided_at is not None:
                continue
            signed = entry.amount if entry.type == LedgerType.charge else -entry.amount
            items.append(BotHistoryItem(
                ts=entry.date,
                kind="ledger",
                title="شارژ نماینده (بدهی)" if entry.type == LedgerType.charge else "پرداخت ثبت‌شده",
                detail=entry.note,
                amount=round(signed, 2),
                account_username=username_by_id.get(entry.account_id),
            ))

    items.sort(key=lambda i: i.ts, reverse=True)
    return items[: max(1, min(limit, 1000))]


def _event_title(ev: AccountEvent) -> str:
    """Persian one-liner for the bot-side AccountEvent kinds that actually
    occur on this path; unknown actions fall through to the raw action so a
    new kind is never rendered as blank."""
    known = {
        "delegate_create": "ساخت اکانت توسط نماینده",
        "delegate_renew": "تمدید توسط نماینده",
        "delegate_extend": "تمدید/افزایش توسط نماینده",
        "delegate_delete": "حذف توسط نماینده",
        "create": "ساخت اکانت",
        "delete": "حذف اکانت",
        "next_plan_activated": "پلن بعدی فعال شد",
        "settle_reset": "تسویه و ریست مصرف",
        "reset": "ریست مصرف",
    }
    if ev.action in known:
        return known[ev.action]
    if ev.action.startswith("delegate_"):
        return f"اقدام نماینده: {ev.action}"
    return ev.action
