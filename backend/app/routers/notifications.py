from fastapi import APIRouter, Depends

from app.auth import require_auth
from app.debt_nudge_job import collect_accruing, collect_overdue, run_debt_nudge

router = APIRouter(prefix="/api/notifications", tags=["notifications"], dependencies=[Depends(require_auth)])


@router.get("/debt-nudge")
async def debt_nudge_preview():
    """Read-only: who the nudge would message right now — the exact list the
    scheduled pass computes, sorted oldest first. The Telegram console's
    list screen re-reads this on every render, so its amounts and the
    already-settled guard are live, never stale from message time.

    `accruing` is the quieter second list — owed but not (yet) nudge-worthy;
    see collect_accruing. Additive: older clients that only read `overdue`
    are unaffected."""
    return {"overdue": collect_overdue(), "accruing": collect_accruing()}


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
