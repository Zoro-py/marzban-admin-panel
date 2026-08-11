import logging
import os
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.backup_job import run_backup
from app.config import settings
from app.db import init_db
from app.payg_monthly_job import maybe_run_monthly_payg_settlement
from app.routers import accounts, auth, backup, customers, groups, ledger, payg_monthly, reports, settings as settings_router, sync
from app.sync_job import run_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def _scheduled_backup() -> None:
    # APScheduler already catches and logs a job's exception on its own, but
    # routes it through its own logger — this makes a failed backup show up
    # in this app's own log output too, at a level (ERROR) that's obvious in
    # `docker compose logs`, not something that requires knowing to also
    # check apscheduler's separate logger.
    try:
        await run_backup()
    except Exception:
        logger.exception("Scheduled backup failed")


async def _scheduled_payg_monthly_settlement() -> None:
    # Same reasoning as _scheduled_backup. Also: a raised exception here is
    # expected and routine, not exceptional — it's exactly how
    # maybe_run_monthly_payg_settlement signals "the Telegram gate failed,
    # don't touch anything" (see payg_monthly_job's own docstring). This is
    # what stops that from also being treated as an unhandled scheduler error.
    try:
        result = await maybe_run_monthly_payg_settlement()
        if result.get("ran"):
            logger.info("Monthly payg settlement: %s", result)
    except Exception:
        logger.exception("Monthly payg settlement failed (will retry tomorrow)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database...")
    init_db()
    logger.info("Starting sync scheduler...")
    # coalesce+max_instances made explicit now that the interval is tight
    # enough for them to matter: if a cycle is still running (e.g. several
    # next-plan activations firing Marzban calls in the same tick) when the
    # next one is due, that next one is skipped rather than queued or run
    # concurrently — so a slow cycle degrades to "less frequent," never to
    # overlapping runs touching the same rows.
    scheduler.add_job(
        run_sync, "interval", seconds=settings.sync_interval_seconds, id="marzban_sync",
        max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        _scheduled_backup, "cron", hour=settings.backup_hour, minute=settings.backup_minute, id="db_backup"
    )
    # Runs daily (not just "on the last day") on purpose — see
    # payg_monthly_job's _target_settlement_period for why that's what makes
    # a failed attempt retry instead of silently skipping a whole month.
    scheduler.add_job(
        _scheduled_payg_monthly_settlement, "cron",
        hour=settings.payg_monthly_settle_hour, minute=settings.payg_monthly_settle_minute,
        id="payg_monthly_settlement",
    )
    scheduler.start()
    yield
    logger.info("Shutting down sync scheduler...")
    scheduler.shutdown()


app = FastAPI(title="VPN Reseller Dashboard API", lifespan=lifespan)

origins_env = os.getenv("CORS_ALLOWED_ORIGINS")
if origins_env:
    allow_origins = [orig.strip() for orig in origins_env.split(",") if orig.strip()]
else:
    # single-operator internal tool; set CORS_ALLOWED_ORIGINS to tighten this
    # once it's exposed beyond a trusted network. No hardcoded placeholder
    # domain here — one that doesn't match the real deployed frontend would
    # silently break every request instead of failing loudly.
    allow_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError):
    return JSONResponse(
        status_code=400,
        content={"detail": "Database integrity error. A related record might be missing or a unique constraint was violated."},
    )

app.include_router(auth.router)
app.include_router(customers.router)
app.include_router(groups.router)
app.include_router(accounts.router)
app.include_router(ledger.router)
app.include_router(reports.router)
app.include_router(sync.router)
app.include_router(backup.router)
app.include_router(payg_monthly.router)
app.include_router(settings_router.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
