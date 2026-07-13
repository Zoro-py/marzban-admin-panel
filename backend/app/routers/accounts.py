import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.auth import require_auth
from app.config import settings
from app.db import get_session
from app.marzban_client import MarzbanAuthError, MarzbanUnavailable, marzban_client
from app.models import Account, AccountEvent, Customer, Group, LedgerEntry, LedgerSource, LedgerType, utcnow
from app.schemas import AccountAdjustRequest, AccountCreateRequest, AccountRead, AccountRelationshipUpdate
from app.services import bytes_from_gb

router = APIRouter(prefix="/api/accounts", tags=["accounts"], dependencies=[Depends(require_auth)])


@router.get("", response_model=list[AccountRead])
def list_accounts(
    unassigned_only: bool = False,
    customer_id: Optional[int] = None,
    group_id: Optional[int] = None,
    session: Session = Depends(get_session),
):
    stmt = select(Account)
    if unassigned_only:
        stmt = stmt.where(Account.customer_id.is_(None), Account.group_id.is_(None))
    if customer_id is not None:
        stmt = stmt.where(Account.customer_id == customer_id)
    if group_id is not None:
        stmt = stmt.where(Account.group_id == group_id)
    return session.exec(stmt).all()


@router.get("/{account_id}", response_model=AccountRead)
def get_account(account_id: int, session: Session = Depends(get_session)):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    return account


@router.post("", response_model=AccountRead)
async def create_account(body: AccountCreateRequest, session: Session = Depends(get_session)):
    existing = session.exec(select(Account).where(Account.marzban_username == body.marzban_username)).first()
    if existing:
        raise HTTPException(409, "This marzban_username is already tracked locally")

    if body.customer_id is not None and not session.get(Customer, body.customer_id):
        raise HTTPException(404, "customer_id not found")
    if body.group_id is not None and not session.get(Group, body.group_id):
        raise HTTPException(404, "group_id not found")

    marzban_payload = {
        "username": body.marzban_username,
        "proxies": body.proxies if body.proxies is not None else settings.marzban_default_proxies,
        "inbounds": body.inbounds if body.inbounds is not None else settings.marzban_default_inbounds,
        "expire": body.expire,
        "data_limit": body.data_limit,
        "data_limit_reset_strategy": body.data_limit_reset_strategy,
        "status": body.status,
        "note": body.note,
    }

    try:
        marzban_user = await marzban_client.create_user(marzban_payload)
    except ValueError as exc:
        raise HTTPException(400, f"Marzban rejected this user: {exc}")
    except (MarzbanUnavailable, MarzbanAuthError) as exc:
        raise HTTPException(502, str(exc))

    account = Account(
        marzban_username=body.marzban_username,
        customer_id=body.customer_id,
        group_id=body.group_id,
        role=body.role,
        rate_per_gb=body.rate_per_gb,
        used_traffic=marzban_user.get("used_traffic", 0),
        lifetime_used_traffic=marzban_user.get("lifetime_used_traffic", 0),
        data_limit=marzban_user.get("data_limit"),
        expire=marzban_user.get("expire"),
        status=marzban_user.get("status"),
        last_synced_at=utcnow(),
    )
    session.add(account)
    session.commit()
    session.refresh(account)

    session.add(AccountEvent(account_id=account.id, action="create", detail="Created via dashboard"))
    session.commit()

    return account


@router.patch("/{account_id}/relationship", response_model=AccountRead)
def update_relationship(account_id: int, body: AccountRelationshipUpdate, session: Session = Depends(get_session)):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")

    changes = body.model_dump(exclude_unset=True)
    if "customer_id" in changes and changes["customer_id"] is not None:
        if not session.get(Customer, changes["customer_id"]):
            raise HTTPException(404, "customer_id not found")
    if "group_id" in changes and changes["group_id"] is not None:
        if not session.get(Group, changes["group_id"]):
            raise HTTPException(404, "group_id not found")

    for field, value in changes.items():
        setattr(account, field, value)
    session.add(account)

    session.add(
        AccountEvent(
            account_id=account.id,
            action="relationship_change",
            detail=str(changes),
        )
    )
    session.commit()
    session.refresh(account)
    return account


@router.post("/{account_id}/adjust", response_model=AccountRead)
async def adjust_account(account_id: int, body: AccountAdjustRequest, session: Session = Depends(get_session)):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")

    payload: dict = {}
    detail_parts: list[str] = []

    if body.set_expire is not None:
        payload["expire"] = body.set_expire
        detail_parts.append(f"set_expire={body.set_expire}")
    elif body.extend_days is not None:
        base = account.expire if account.expire else int(time.time())
        payload["expire"] = base + body.extend_days * 86400
        detail_parts.append(f"extend_days={body.extend_days}")

    if body.set_data_limit_gb is not None:
        payload["data_limit"] = bytes_from_gb(body.set_data_limit_gb)
        detail_parts.append(f"set_data_limit_gb={body.set_data_limit_gb}")
    elif body.extend_gb is not None:
        base = account.data_limit or 0
        payload["data_limit"] = max(0, base + bytes_from_gb(body.extend_gb))
        detail_parts.append(f"extend_gb={body.extend_gb}")

    if not payload:
        raise HTTPException(400, "Provide at least one of extend_days/extend_gb/set_expire/set_data_limit_gb")

    try:
        marzban_user = await marzban_client.modify_user(account.marzban_username, payload)
    except ValueError as exc:
        raise HTTPException(400, f"Marzban rejected this change: {exc}")
    except (MarzbanUnavailable, MarzbanAuthError) as exc:
        raise HTTPException(502, str(exc))

    account.expire = marzban_user.get("expire", account.expire)
    account.data_limit = marzban_user.get("data_limit", account.data_limit)
    account.status = marzban_user.get("status", account.status)
    account.last_synced_at = utcnow()
    session.add(account)

    session.add(
        AccountEvent(
            account_id=account.id,
            action="adjust",
            detail=", ".join(detail_parts) + (f" | note={body.note}" if body.note else ""),
        )
    )
    session.commit()
    session.refresh(account)
    return account


@router.get("/{account_id}/invoice")
def get_account_invoice(account_id: int, session: Session = Depends(get_session)):
    """Standalone (non-group) pay-as-you-go preview for one account."""
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    billable_bytes = max(0, account.lifetime_used_traffic - account.usage_baseline)
    billable_gb = billable_bytes / (1024**3)
    rate = account.rate_per_gb or 0
    return {
        "account_id": account_id,
        "since": account.usage_baseline_at,
        "billable_gb": round(billable_gb, 3),
        "rate_per_gb": rate,
        "amount": round(billable_gb * rate, 2),
    }


@router.post("/{account_id}/settle")
def settle_account(account_id: int, session: Session = Depends(get_session)):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    if account.group_id is not None:
        raise HTTPException(400, "This account is billed through its group — use /api/groups/{group_id}/settle")

    billable_bytes = max(0, account.lifetime_used_traffic - account.usage_baseline)
    billable_gb = billable_bytes / (1024**3)
    rate = account.rate_per_gb or 0
    amount = round(billable_gb * rate, 2)

    now = utcnow()
    if amount > 0:
        session.add(
            LedgerEntry(
                type=LedgerType.charge,
                amount=amount,
                customer_id=account.customer_id,
                account_id=account.id,
                note=f"Usage settlement for cycle ending {now.date().isoformat()}",
                source=LedgerSource.web,
            )
        )

    account.usage_baseline = account.lifetime_used_traffic
    account.usage_baseline_at = now
    session.add(account)
    session.commit()

    return {"account_id": account_id, "charged_amount": amount, "settled_at": now}
