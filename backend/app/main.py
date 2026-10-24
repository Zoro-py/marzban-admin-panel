import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.backup_job import run_backup
from app.config import settings
from app.db import init_db
from app.debt_nudge_job import run_debt_nudge
from app.payg_monthly_job import maybe_run_monthly_payg_settlement
from app.routers import accounts, auth, backup, connections, customers, delegate, groups, history, ledger, monitor, notifications, payg_monthly, reports, settings as settings_router, shop, sub_stats, sync
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


async def _scheduled_shop_order_sweep() -> None:
    """Refunds self-serve orders that took a customer's money and never
    finished provisioning — which is what a process kill between the wallet
    debit and Marzban's reply leaves behind. Runs on a schedule rather than
    only at startup because a container that restarts cleanly is not the only
    way an order gets stranded (see shop_service.purchase's ordering note)."""
    from app.db import get_session
    from app.shop_service import sweep_stuck_orders
    try:
        session_gen = get_session()
        session = next(session_gen)
        try:
            resolved = await sweep_stuck_orders(session)
            if resolved:
                logger.warning("Resolved %d stuck shop order(s)", len(resolved))
        finally:
            session_gen.close()
    except Exception:
        logger.exception("Shop stuck-order sweep failed")


async def _scheduled_shop_renewal_warnings() -> None:
    """Tells shop customers their service is about to end, while it still
    works. Without it the month simply ran out and the customer's first news
    was a VPN that had stopped connecting — the worst possible moment to ask
    them to buy again, and the point at which most of them didn't."""
    from app.db import get_session
    from app.shop_service import (
        _sweep_debt_accounts,
        notify_overdue_payments,
        reactivate_suspended_payg_accounts,
        warn_customers_before_service_ends,
    )
    try:
        session_gen = get_session()
        session = next(session_gen)
        try:
            sent = await warn_customers_before_service_ends(session)
            if sent:
                logger.info("Sent %d shop renewal warning(s)", sent)
            # Same pass, same cadence: both are "tell the customer before they
            # have to wonder". A payment past its promised approval time is
            # announced as late rather than left to look abandoned.
            late = await notify_overdue_payments(session)
            if late:
                logger.info("Told %d customer(s) their payment is running late", late)
            # Also here: the bounded-debt auto-renew's own reminder/suspend/
            # reactivate pass (see shop_service.py) — day-based thresholds,
            # same reasoning as the warnings above for why 15 minutes is
            # precise enough.
            debt = await _sweep_debt_accounts(session)
            if debt["reminded"] or debt["suspended"] or debt["reactivated"]:
                logger.info("Auto-renew debt sweep: %d reminded, %d suspended, %d reactivated",
                           debt["reminded"], debt["suspended"], debt["reactivated"])
            # F7: a payg service the meter suspended for an empty wallet comes
            # back here (within 15 minutes of a top-up) instead of waiting for
            # tomorrow's daily meter — the suspension message promises "به محض
            # شارژ کیف پول، خودش دوباره فعال می‌شود", and a day is not "به محض".
            reactivated = await reactivate_suspended_payg_accounts(session)
            if reactivated:
                logger.info("Reactivated %d funded payg account(s)", reactivated)
        finally:
            session_gen.close()
    except Exception:
        logger.exception("Shop renewal-warning pass failed")


async def _scheduled_delegate_lifecycle_warnings() -> None:
    """F15: DMs each delegate whose scoped account is 3 days from expiry or
    at 80% usage — the delegate-side mirror of the shop renewal warnings
    above (same thresholds, same once-per-episode discipline, same 15-minute
    cadence, which is far denser than day-granular warnings need but costs
    nothing because a delivered warning records its own AccountEvent marker).
    Best-effort: see warn_delegates_before_service_ends — failures are logged
    and retried on a later pass, never fatal to the scheduler."""
    from app.db import get_session
    from app.delegate_service import warn_delegates_before_service_ends
    try:
        session_gen = get_session()
        session = next(session_gen)
        try:
            sent = await warn_delegates_before_service_ends(session)
            if sent:
                logger.info("Sent %d delegate lifecycle warning(s)", sent)
        finally:
            session_gen.close()
    except Exception:
        logger.exception("Delegate lifecycle-warning pass failed")


async def _scheduled_debt_nudge() -> None:
    # Every OTHER day: the cron fires daily at the configured hour and this
    # skips odd calendar dates (date.toordinal parity) — a plain cron can't
    # express "every 2 days" without drifting at month boundaries, and a
    # stateless parity check never needs "did it run yesterday" tracking.
    if datetime.now(timezone.utc).toordinal() % 2 != 0:
        return
    # Purely informational (see debt_nudge_job's own docstring) — a failure
    # here just means this run is silent; the next scheduled run tries
    # again on its own, no retry/self-healing state needed the way the
    # money-moving jobs above require.
    try:
        result = await run_debt_nudge()
        if result.get("sent"):
            logger.info("Debt nudge sent: %s", result)
    except Exception:
        logger.exception("Debt nudge failed (will try again next run)")


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


async def _scheduled_payg_retail_meter() -> None:
    """Retail pay-as-you-go's own daily meter (distinct from the B2B
    monthly settlement above) — see shop_service._sweep_payg_accounts."""
    from app.db import get_session
    from app.shop_service import _sweep_payg_accounts
    try:
        session_gen = get_session()
        session = next(session_gen)
        try:
            result = await _sweep_payg_accounts(session)
            if any(result.values()):
                logger.info("Payg retail meter: %s", result)
        finally:
            session_gen.close()
    except Exception:
        logger.exception("Payg retail meter failed (will retry tomorrow)")


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
    # Every 5 minutes: well inside STUCK_ORDER_TIMEOUT_MINUTES, so a stranded
    # order is refunded within about fifteen minutes of the customer paying
    # for it rather than sitting until someone notices.
    scheduler.add_job(
        _scheduled_shop_order_sweep, "interval", minutes=5, id="shop_order_sweep",
        max_instances=1, coalesce=True,
    )
    # Every 15 minutes. The thresholds are days (paid plans) and hours
    # (trials), so this is precise enough, and each warning fires at most once
    # per order no matter how often the pass runs.
    scheduler.add_job(
        _scheduled_shop_renewal_warnings, "interval", minutes=15, id="shop_renewal_warnings",
        max_instances=1, coalesce=True,
    )
    # F15: same 15-minute cadence as the shop warnings it mirrors — a
    # delivered warning records its AccountEvent marker, so the dense schedule
    # never spams (each account is warned once per episode no matter how often
    # this fires).
    scheduler.add_job(
        _scheduled_delegate_lifecycle_warnings, "interval", minutes=15, id="delegate_lifecycle_warnings",
        max_instances=1, coalesce=True,
    )
    # Runs daily (not just "on the last day") on purpose — see
    # payg_monthly_job's _target_settlement_period for why that's what makes
    # a failed attempt retry instead of silently skipping a whole month.
    scheduler.add_job(
        _scheduled_payg_monthly_settlement, "cron",
        hour=settings.payg_monthly_settle_hour, minute=settings.payg_monthly_settle_minute,
        id="payg_monthly_settlement",
    )
    scheduler.add_job(
        _scheduled_payg_retail_meter, "cron",
        hour=settings.payg_retail_meter_hour, minute=settings.payg_retail_meter_minute,
        id="payg_retail_meter",
    )
    # Fires daily at the configured hour; the job itself skips every other
    # calendar day (see _scheduled_debt_nudge) — the operator asked for
    # every-other-day reminders. Still a duration check, not an amount one:
    # only debt that has genuinely sat for DEBT_NUDGE_MIN_DAYS gets raised
    # (see debt_nudge_job's docstring).
    scheduler.add_job(
        _scheduled_debt_nudge, "cron",
        hour=settings.debt_nudge_hour, minute=settings.debt_nudge_minute,
        id="debt_nudge",
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
app.include_router(history.router)
app.include_router(reports.router)
app.include_router(sync.router)
app.include_router(backup.router)
app.include_router(notifications.router)
app.include_router(payg_monthly.router)
app.include_router(settings_router.router)
app.include_router(shop.router)
app.include_router(shop.bot_router)
app.include_router(delegate.router)
app.include_router(monitor.router)
app.include_router(monitor.ingest_router)
app.include_router(delegate.bot_router)
app.include_router(sub_stats.router)
app.include_router(connections.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
