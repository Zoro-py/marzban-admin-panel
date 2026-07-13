from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import init_db
from app.routers import accounts, auth, customers, groups, ledger, reports, sync
from app.sync_job import run_sync

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.add_job(run_sync, "interval", minutes=settings.sync_interval_minutes, id="marzban_sync")
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="VPN Reseller Dashboard API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # single-operator internal tool; tighten if exposed publicly
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(customers.router)
app.include_router(groups.router)
app.include_router(accounts.router)
app.include_router(ledger.router)
app.include_router(reports.router)
app.include_router(sync.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
