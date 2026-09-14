from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.auth import require_auth
from app.db import get_session
from app.services import get_settings

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(require_auth)])


class SettingsRead(BaseModel):
    default_rate_per_gb: float | None


class SettingsUpdate(BaseModel):
    # A negative or non-finite rate would flow straight into every charge.
    default_rate_per_gb: float | None = Field(default=None, ge=0, allow_inf_nan=False)


@router.get("", response_model=SettingsRead)
def read_settings(session: Session = Depends(get_session)):
    return get_settings(session)


@router.patch("", response_model=SettingsRead)
def update_settings(body: SettingsUpdate, session: Session = Depends(get_session)):
    settings = get_settings(session)
    settings.default_rate_per_gb = body.default_rate_per_gb
    session.add(settings)
    session.commit()
    session.refresh(settings)
    return settings
