"""Delegated self-service — see models.py's Delegate docstring.

TWO ROUTERS, TWO DIFFERENT AUTH BOUNDARIES, same shape as routers/shop.py:

  router      /api/delegate/*      operator only, same JWT as the rest of
                                   the dashboard. Creates/edits/revokes grants.
  bot_router  /api/delegate/bot/*  delegate_bot, holding ONLY
                                   DELEGATE_BOT_API_KEY — reaches nothing but
                                   these endpoints, never the ledger directly,
                                   never Marzban admin credentials.

Do not "simplify" this by putting the bot endpoints behind require_auth and
handing delegate_bot the Marzban admin credentials — that is precisely the
consolidation this split exists to prevent (see shopbot's own docstring for
the same reasoning against the same shortcut).

Every bot_router endpoint identifies the delegate by telegram_id and re-
derives their scope from the Delegate row on every call — see
delegate_service.py's module docstring for why an account_id alone is never
trusted.
"""

from __future__ import annotations

import logging
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlmodel import Session, select

from app.auth import require_auth
from app.config import settings as app_settings
from app.db import get_session
from app.delegate_service import (
    DelegateError,
    create_delegate_account,
    delete_delegate_account,
    get_active_delegate,
    list_delegate_accounts,
    renew_delegate_account,
    scope_name,
)
from app.models import Customer, Delegate, Group
from app.schemas import (
    DelegateAccountCreateRequest,
    DelegateAccountDeleteRequest,
    DelegateAccountRenewRequest,
    DelegateAccountRow,
    DelegateCreateRequest,
    DelegateRead,
    DelegateSession,
    DelegateSessionRequest,
)

logger = logging.getLogger(__name__)


def require_delegate_bot(x_delegate_bot_key: Optional[str] = Header(default=None)) -> None:
    """Fails CLOSED when DELEGATE_BOT_API_KEY is unset — same reasoning as
    routers/shop.py's require_shop_bot: an unset key means the operator
    hasn't configured delegate_bot, and the safe reading of that is "this
    surface isn't in use," not "let anyone in." compare_digest on bytes for
    the same timing-leak reason documented there."""
    expected = app_settings.delegate_bot_api_key
    if not expected:
        raise HTTPException(503, "Delegate bot API is not configured on this server")
    if not x_delegate_bot_key:
        raise HTTPException(401, "Invalid delegate bot key")
    if not secrets.compare_digest(x_delegate_bot_key.encode("utf-8", "surrogateescape"),
                                  expected.encode("utf-8")):
        raise HTTPException(401, "Invalid delegate bot key")


router = APIRouter(prefix="/api/delegate", tags=["delegate"], dependencies=[Depends(require_auth)])
bot_router = APIRouter(
    prefix="/api/delegate/bot", tags=["delegate-bot"], dependencies=[Depends(require_delegate_bot)]
)


def _read(session: Session, delegate: Delegate) -> DelegateRead:
    return DelegateRead(
        id=delegate.id,
        customer_id=delegate.customer_id,
        group_id=delegate.group_id,
        scope_name=scope_name(session, delegate),
        telegram_id=delegate.telegram_id,
        label=delegate.label,
        is_active=delegate.is_active,
        credit_limit=delegate.credit_limit,
        daily_create_cap=delegate.daily_create_cap,
        username_prefix=delegate.username_prefix,
        default_duration_days=delegate.default_duration_days,
        created_at=delegate.created_at,
    )


def _account_row(account) -> DelegateAccountRow:
    return DelegateAccountRow(
        id=account.id,
        marzban_username=account.marzban_username,
        used_traffic=account.used_traffic,
        data_limit=account.data_limit,
        expire=account.expire,
        status=account.status,
        subscription_url=account.subscription_url,
        created_at=account.created_at,
    )


# ══════════════════════════════════════════════════════════ operator-only


@router.get("", response_model=list[DelegateRead])
def list_delegates(session: Session = Depends(get_session)):
    delegates = session.exec(select(Delegate)).all()
    return [_read(session, d) for d in delegates]


@router.post("", response_model=DelegateRead)
def create_or_update_delegate(body: DelegateCreateRequest, session: Session = Depends(get_session)):
    if (body.customer_id is None) == (body.group_id is None):
        raise HTTPException(400, "Provide exactly one of customer_id or group_id")
    if body.customer_id is not None and not session.get(Customer, body.customer_id):
        raise HTTPException(404, "customer_id not found")
    if body.group_id is not None and not session.get(Group, body.group_id):
        raise HTTPException(404, "group_id not found")

    delegate = session.exec(select(Delegate).where(Delegate.telegram_id == body.telegram_id)).first()
    if delegate is None:
        delegate = Delegate(telegram_id=body.telegram_id)
    delegate.customer_id = body.customer_id
    delegate.group_id = body.group_id
    delegate.label = body.label
    delegate.credit_limit = body.credit_limit
    delegate.daily_create_cap = body.daily_create_cap
    delegate.username_prefix = body.username_prefix
    delegate.default_duration_days = body.default_duration_days
    delegate.is_active = True
    session.add(delegate)
    session.commit()
    session.refresh(delegate)
    return _read(session, delegate)


@router.post("/{delegate_id}/deactivate", response_model=DelegateRead)
def deactivate_delegate(delegate_id: int, session: Session = Depends(get_session)):
    delegate = session.get(Delegate, delegate_id)
    if not delegate:
        raise HTTPException(404, "Delegate not found")
    delegate.is_active = False
    session.add(delegate)
    session.commit()
    session.refresh(delegate)
    return _read(session, delegate)


# ══════════════════════════════════════════════════════════ delegate_bot


def _require_delegate(session: Session, telegram_id: int) -> Delegate:
    delegate = get_active_delegate(session, telegram_id)
    if delegate is None:
        raise HTTPException(403, "این تلگرام آیدی دسترسی فعالی ندارد.")
    return delegate


@bot_router.post("/session", response_model=DelegateSession)
def bot_session(body: DelegateSessionRequest, session: Session = Depends(get_session)):
    delegate = _require_delegate(session, body.telegram_id)
    return DelegateSession(
        delegate_id=delegate.id,
        label=delegate.label,
        scope_name=scope_name(session, delegate),
        default_duration_days=delegate.default_duration_days,
        quick_volumes_gb=[10, 20, 30, 50, 100],
    )


@bot_router.get("/accounts", response_model=list[DelegateAccountRow])
def bot_list_accounts(telegram_id: int, session: Session = Depends(get_session)):
    delegate = _require_delegate(session, telegram_id)
    accounts = list_delegate_accounts(session, delegate)
    return [_account_row(a) for a in accounts]


@bot_router.post("/accounts", response_model=DelegateAccountRow)
async def bot_create_account(body: DelegateAccountCreateRequest, session: Session = Depends(get_session)):
    delegate = _require_delegate(session, body.telegram_id)
    try:
        account = await create_delegate_account(session, delegate, body.data_limit_gb)
    except DelegateError as exc:
        raise HTTPException(400, str(exc))
    return _account_row(account)


@bot_router.post("/accounts/{account_id}/renew", response_model=DelegateAccountRow)
async def bot_renew_account(account_id: int, body: DelegateAccountRenewRequest, session: Session = Depends(get_session)):
    delegate = _require_delegate(session, body.telegram_id)
    try:
        account = await renew_delegate_account(session, delegate, account_id, body.extend_gb, body.extend_days)
    except DelegateError as exc:
        raise HTTPException(400, str(exc))
    return _account_row(account)


@bot_router.post("/accounts/{account_id}/delete")
async def bot_delete_account(account_id: int, body: DelegateAccountDeleteRequest, session: Session = Depends(get_session)):
    delegate = _require_delegate(session, body.telegram_id)
    try:
        await delete_delegate_account(session, delegate, account_id)
    except DelegateError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}
