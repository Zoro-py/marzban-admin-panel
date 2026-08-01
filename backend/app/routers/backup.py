from fastapi import APIRouter, Depends, HTTPException

from app.auth import require_auth
from app.backup_job import run_backup

router = APIRouter(prefix="/api/backup", tags=["backup"], dependencies=[Depends(require_auth)])


@router.post("/run")
async def trigger_backup():
    """Runs the same backup the nightly schedule runs, immediately — lets an
    operator confirm the whole pipeline (DB copy, zip, Telegram delivery)
    actually works right after setting it up, instead of finding out at
    3:30am whether it's broken."""
    try:
        return await run_backup()
    except Exception as exc:
        raise HTTPException(502, str(exc))
