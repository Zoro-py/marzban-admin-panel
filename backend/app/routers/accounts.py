import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.auth import require_auth
from app.config import settings
from app.db import get_session
from app.marzban_client import MarzbanAuthError, MarzbanUnavailable, marzban_client
from app.models import Account, AccountEvent, BillingMode, Customer, Group, LedgerEntry, LedgerSource, LedgerType, utcnow
from app.schemas import (
    AccountAdjustRequest,
    AccountBillingUpdate,
    AccountCreateRequest,
    AccountEventRead,
    AccountRead,
    AccountRelationshipUpdate,
    AccountResetRequest,
    AccountRow,
)
from app.services import bytes_from_gb, effective_rate, enrich_accounts

router = APIRouter(prefix="/api/accounts", tags=["accounts"], dependencies=[Depends(require_auth)])


@router.get("", response_model=list[AccountRow])
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
    accounts = session.exec(stmt).all()
    return enrich_accounts(session, accounts)


@router.get("/{account_id}", response_model=AccountRow)
def get_account(account_id: int, session: Session = Depends(get_session)):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    return enrich_accounts(session, [account])[0]


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

    now = utcnow()
    account = Account(
        marzban_username=body.marzban_username,
        customer_id=body.customer_id,
        group_id=body.group_id,
        role=body.role,
        rate_per_gb=body.rate_per_gb,
        used_traffic=marzban_user.get("used_traffic", 0),
        lifetime_used_traffic=marzban_user.get("lifetime_used_traffic", 0),
        first_seen_traffic=marzban_user.get("lifetime_used_traffic", 0),
        first_seen_traffic_at=now,
        # This account was just created via marzban_client.create_user() above,
        # so its lifetime usage is always genuinely 0 here — usage_baseline is
        # deliberately left at the model default (0) rather than mirrored from
        # lifetime, matching sync_job.py's policy (see its comment): billing
        # should never start from "whatever Marzban already reports," only
        # from real observed usage.
        usage_baseline_at=now,
        data_limit=marzban_user.get("data_limit"),
        expire=marzban_user.get("expire"),
        status=marzban_user.get("status"),
        last_synced_at=now,
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


@router.patch("/{account_id}/billing", response_model=AccountRead)
def update_billing(account_id: int, body: AccountBillingUpdate, session: Session = Depends(get_session)):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")

    if body.clear_rate:
        account.rate_per_gb = None
    elif body.rate_per_gb is not None:
        account.rate_per_gb = body.rate_per_gb

    if body.billing_mode is not None:
        account.billing_mode = body.billing_mode

    session.add(account)
    session.add(
        AccountEvent(
            account_id=account.id,
            action="billing_change",
            detail=f"rate_per_gb={account.rate_per_gb}, billing_mode={account.billing_mode}",
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


@router.get("/{account_id}/events", response_model=list[AccountEventRead])
def get_account_events(account_id: int, limit: int = 50, session: Session = Depends(get_session)):
    """The audit trail (adjust/reset/billing/ownership changes) that was being
    written since day one but never exposed — the account inspector's History
    section reads it, merged client-side with this account's ledger entries."""
    if not session.get(Account, account_id):
        raise HTTPException(404, "Account not found")
    stmt = (
        select(AccountEvent)
        .where(AccountEvent.account_id == account_id)
        .order_by(AccountEvent.date.desc(), AccountEvent.id.desc())
        .limit(limit)
    )
    return session.exec(stmt).all()


@router.get("/{account_id}/invoice")
def get_account_invoice(account_id: int, session: Session = Depends(get_session)):
    """Standalone (non-group) pay-as-you-go preview for one account."""
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    billable_bytes = max(0, account.lifetime_used_traffic - account.usage_baseline)
    billable_gb = billable_bytes / (1024**3)
    rate = effective_rate(session, account)
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
    rate = effective_rate(session, account)
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


@router.post("/{account_id}/reset", response_model=AccountRead)
async def reset_account(account_id: int, body: AccountResetRequest, session: Session = Depends(get_session)):
    """Starts a new usage cycle in Marzban. If `charge_amount` is explicitly
    given (including 0, to deliberately skip charging e.g. a comp reset), that
    exact value is posted. Otherwise, for a payg account, the accrued usage is
    computed and charged automatically — resetting always rolls the billing
    baseline forward regardless, so leaving this to silently charge nothing
    would permanently lose that cycle's billing data."""
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")

    charge_amount = body.charge_amount
    if charge_amount is None and account.billing_mode == BillingMode.payg:
        billable_bytes = max(0, account.lifetime_used_traffic - account.usage_baseline)
        billable_gb = billable_bytes / (1024**3)
        charge_amount = round(billable_gb * effective_rate(session, account), 2)

    if charge_amount and charge_amount > 0 and not account.customer_id and not account.group_id:
        raise HTTPException(400, "Can't charge an unassigned account — assign it to a customer first")

    try:
        marzban_user = await marzban_client.reset_user(account.marzban_username)
    except ValueError as exc:
        raise HTTPException(400, f"Marzban rejected this reset: {exc}")
    except (MarzbanUnavailable, MarzbanAuthError) as exc:
        raise HTTPException(502, str(exc))

    now = utcnow()
    if charge_amount and charge_amount > 0:
        session.add(
            LedgerEntry(
                type=LedgerType.charge,
                amount=round(charge_amount, 2),
                customer_id=account.customer_id,
                group_id=account.group_id,
                account_id=account.id,
                note=body.note or f"Usage reset for cycle ending {now.date().isoformat()}",
                source=LedgerSource.web,
            )
        )

    account.used_traffic = marzban_user.get("used_traffic", 0)
    account.lifetime_used_traffic = marzban_user.get("lifetime_used_traffic", account.lifetime_used_traffic)
    account.expire = marzban_user.get("expire", account.expire)
    account.data_limit = marzban_user.get("data_limit", account.data_limit)
    account.status = marzban_user.get("status", account.status)
    # Reset always rolls the billing baseline forward too, regardless of billing_mode
    # or whether a charge was posted — keeps it consistent if billing_mode changes later.
    account.usage_baseline = account.lifetime_used_traffic
    account.usage_baseline_at = now
    account.last_synced_at = now
    session.add(account)

    session.add(
        AccountEvent(
            account_id=account.id,
            action="reset",
            detail=f"charge_amount={charge_amount}" + (f" | note={body.note}" if body.note else ""),
        )
    )
    session.commit()
    session.refresh(account)
    return account
