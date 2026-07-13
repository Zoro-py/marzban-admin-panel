from fastapi import APIRouter, Depends, HTTPException

from app.auth import require_auth
from app.marzban_client import MarzbanAuthError, MarzbanUnavailable
from app.sync_job import run_sync

router = APIRouter(prefix="/api/sync", tags=["sync"], dependencies=[Depends(require_auth)])


@router.post("/run")
async def trigger_sync():
    try:
        return await run_sync()
    except (MarzbanUnavailable, MarzbanAuthError) as exc:
        raise HTTPException(502, str(exc))
