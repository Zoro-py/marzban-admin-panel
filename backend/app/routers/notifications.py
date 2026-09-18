from fastapi import APIRouter, Depends

from app.auth import require_auth
from app.debt_nudge_job import run_debt_nudge

router = APIRouter(prefix="/api/notifications", tags=["notifications"], dependencies=[Depends(require_auth)])


@router.post("/debt-nudge/run")
async def trigger_debt_nudge():
    """Runs the same debt-nudge pass the every-other-day schedule runs,
    immediately — the manual twin of /api/backup/run: lets the operator send
    the reminder the moment they want it (or check the whole pipeline — debt
    aging, Telegram delivery — right after setup) instead of waiting for the
    next scheduled morning. The job's own best-effort contract holds: a
    Telegram failure comes back as sent=false with the error string rather
    than an HTTP 5xx, so the panel can put it in a toast instead of a crash
    dialog."""
    return await run_debt_nudge()
