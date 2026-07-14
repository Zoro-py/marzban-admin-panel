from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, func, select

from app.auth import require_auth
from app.db import get_session
from app.marzban_client import MarzbanAuthError, MarzbanUnavailable
from app.models import Account
from app.sync_job import run_sync

router = APIRouter(prefix="/api/sync", tags=["sync"], dependencies=[Depends(require_auth)])


@router.post("/run")
async def trigger_sync():
    try:
        return await run_sync()
    except (MarzbanUnavailable, MarzbanAuthError) as exc:
        raise HTTPException(502, str(exc))


@router.get("/status")
def sync_status(session: Session = Depends(get_session)):
    """When the mirror last heard from Marzban — the shell shows this next to
    the manual sync button so "is this data fresh?" never has to be guessed.
    max(last_synced_at) across accounts IS the last completed sync: run_sync
    stamps every account (created or updated) with the same `now` each run."""
    last = session.exec(select(func.max(Account.last_synced_at))).one()
    count = session.exec(select(func.count()).select_from(Account)).one()
    return {"last_synced_at": last, "account_count": count}
