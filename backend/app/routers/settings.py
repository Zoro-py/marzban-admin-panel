from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.auth import require_auth
from app.db import get_session
from app.models import RateChange
from app.services import get_settings

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(require_auth)])


class SettingsRead(BaseModel):
    default_rate_per_gb: float | None


class SettingsUpdate(BaseModel):
    # A negative or non-finite rate would flow straight into every charge.
    default_rate_per_gb: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class RateChangeRead(BaseModel):
    """One structured rate-change audit row (see models.RateChange)."""
    id: int
    scope: Literal["account", "group", "default"]
    account_id: Optional[int] = None
    group_id: Optional[int] = None
    old_rate: Optional[float] = None  # NULL = was unset (inherited), not zero
    new_rate: Optional[float] = None  # NULL = cleared (inherited from now on)
    created_by: Optional[str] = None
    created_at: datetime


@router.get("", response_model=SettingsRead)
def read_settings(session: Session = Depends(get_session)):
    return get_settings(session)


@router.patch("", response_model=SettingsRead)
def update_settings(body: SettingsUpdate, session: Session = Depends(get_session), operator: str = Depends(require_auth)):
    settings = get_settings(session)
    old_rate = settings.default_rate_per_gb
    settings.default_rate_per_gb = body.default_rate_per_gb
    session.add(settings)
    if old_rate != settings.default_rate_per_gb:
        # Structured audit trail for the dashboard-wide default (see
        # models.RateChange) — this endpoint previously logged NOTHING at
        # all, which is exactly how "was the global rate ever different?"
        # became unanswerable.
        session.add(RateChange(
            scope="default",
            old_rate=old_rate,
            new_rate=settings.default_rate_per_gb,
            created_by=operator,
        ))
    session.commit()
    session.refresh(settings)
    return settings


@router.get("/rate-changes", response_model=list[RateChangeRead])
def read_rate_changes(
    account_id: int | None = None,
    group_id: int | None = None,
    session: Session = Depends(get_session),
):
    """Rate-change history for one scope: an account's own rate changes, a
    group's, or (with neither param) the dashboard-wide default's. Note this
    deliberately does NOT mix scopes: an account's history is its own
    rate_per_gb field only — what the account actually INHERITS (group rate,
    then default) reads from the effective_rate chain, and mixing the three
    here would suggest a causal link that isn't in the data."""
    stmt = select(RateChange).order_by(RateChange.created_at.desc(), RateChange.id.desc()).limit(50)
    if account_id is not None:
        stmt = stmt.where(RateChange.scope == "account", RateChange.account_id == account_id)
    elif group_id is not None:
        stmt = stmt.where(RateChange.scope == "group", RateChange.group_id == group_id)
    else:
        stmt = stmt.where(RateChange.scope == "default")
    return session.exec(stmt).all()
