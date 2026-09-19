"""Server monitoring ingest + reporting.

TWO AUTH BOUNDARIES, same shape as shop.py:

  router (this one)  /api/monitor/servers|events|history   operator only,
                       the dashboard JWT — the "one place to see every
                       server's health" page.
  ingest endpoint     POST /api/monitor/ingest             the monitoring
                       agents on the VPN servers, holding ONLY the
                       MONITOR_INGEST_TOKEN shared secret.

The agents run as root on the VPN boxes and cross the public internet to
reach the panel, so they get a narrow token that can INSERT metrics and
events and nothing else — not the ledger, not Marzban, not even reading the
metrics back. Fail closed on an unset token (see config.monitor_ingest_token).

Retention / the 1GB log budget: pruning happens HERE, on ingest, because
ingest is the only guaranteed-to-run-regularly write path (a dashboard nobody
opens must not be what keeps the database small). The DELETEs are indexed
range deletes of a handful of rows per minute — cheap on WAL SQLite.

This router touches NO billing logic: ServerMetric/MonitorEvent are written
by agents and read by the dashboard, and nothing else joins to them.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlmodel import Session, func, select

from app.auth import require_auth
from app.config import settings
from app.db import get_session
from app.models import MonitorEvent, ServerMetric
from app.schemas import MonitorIngestIn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/monitor", tags=["monitor"])

# A server whose newest sample is older than this shows as "stale" rather
# than "online" on the dashboard: two missed minutes is network jitter or a
# busy box, not an outage — claiming "offline" that early would cry wolf.
STALE_AFTER = timedelta(minutes=5)

# History queries are windowed, not paginated, so their size is bounded by
# the window: 7 days of 1-minute samples is ~10k points/server. Anything
# longer than that is a job for the local agent logs, not the dashboard.
MAX_HISTORY_HOURS = 168


def _require_monitor_token(x_monitor_token: Optional[str] = Header(default=None)) -> None:
    expected = settings.monitor_ingest_token
    if not expected or not x_monitor_token or not secrets.compare_digest(x_monitor_token, expected):
        # Same response for "no token configured" and "wrong token": the
        # endpoint must not reveal which one the caller got wrong.
        raise HTTPException(status_code=401, detail="Invalid monitor token")


ingest_router = APIRouter(prefix="/api/monitor", tags=["monitor"], dependencies=[Depends(_require_monitor_token)])


def _prune(session: Session) -> None:
    """Enforce the retention halves of the 1GB log budget (see config)."""
    from sqlmodel import delete  # noqa: F401 — re-exported by sqlmodel

    metric_cutoff = datetime.now(timezone.utc) - timedelta(days=settings.monitor_metric_retention_days)
    event_cutoff = datetime.now(timezone.utc) - timedelta(days=settings.monitor_event_retention_days)
    session.exec(delete(ServerMetric).where(ServerMetric.ts < metric_cutoff))
    session.exec(delete(MonitorEvent).where(MonitorEvent.ts < event_cutoff))


@ingest_router.post("/ingest")
def ingest(payload: MonitorIngestIn, session: Session = Depends(get_session)):
    stored_metrics = 0
    if payload.metrics is not None:
        m = payload.metrics
        session.add(ServerMetric(server_id=payload.server_id, **m.model_dump(exclude={"extra"}), extra=json.dumps(m.extra)))
        stored_metrics = 1

    for ev in payload.events:
        session.add(MonitorEvent(server_id=payload.server_id, **ev.model_dump()))

    # Prune on every ingest — see module docstring for why this runs here
    # and not in a dashboard-triggered path.
    _prune(session)
    session.commit()
    return {"stored_metrics": stored_metrics, "stored_events": len(payload.events)}


def _server_status(last_ts: Optional[datetime]) -> str:
    if last_ts is None:
        return "unknown"
    # SQLite gives naive datetimes back; normalize to aware before comparing.
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - last_ts
    if age > STALE_AFTER:
        return "stale"
    return "online"


@router.get("/servers", dependencies=[Depends(require_auth)])
def servers(session: Session = Depends(get_session)):
    """Latest snapshot per server plus 24h event counts — the payload behind
    the dashboard's server cards. One small query per known server_id (there
    are five) rather than a window-function dance: clearer, and cheap."""
    metric_ids = set(session.exec(select(ServerMetric.server_id).distinct()).all())
    event_ids = set(session.exec(select(MonitorEvent.server_id).distinct()).all())
    result = []
    for sid in sorted(metric_ids | event_ids):
        last = session.exec(
            select(ServerMetric).where(ServerMetric.server_id == sid).order_by(ServerMetric.ts.desc())  # type: ignore[attr-defined]
        ).first()
        warn_24h = session.exec(
            select(func.count()).select_from(MonitorEvent).where(
                MonitorEvent.server_id == sid,
                MonitorEvent.ts > datetime.now(timezone.utc) - timedelta(hours=24),
                MonitorEvent.severity != "info",
            )
        ).one()
        last_event = session.exec(
            select(MonitorEvent).where(MonitorEvent.server_id == sid).order_by(MonitorEvent.ts.desc())  # type: ignore[attr-defined]
        ).first()
        if last is not None:
            row = last.model_dump()
            row["extra"] = json.loads(last.extra or "{}")
            row["status"] = _server_status(last.ts)
        else:
            # Events but not a single metric yet (brand-new agent, or one
            # whose metric half is failing): still show the server, marked
            # unknown — hiding a box that is reporting problems is worse.
            row = ServerMetric(server_id=sid, ts=datetime.now(timezone.utc)).model_dump()
            row["extra"] = {}
            row["status"] = "unknown"
        row["warn_events_24h"] = warn_24h
        row["last_event"] = (
            {"ts": last_event.ts, "type": last_event.type, "severity": last_event.severity, "detail": last_event.detail}
            if last_event
            else None
        )
        result.append(row)
    # Panel first: it is the box everything else reports THROUGH, so an
    # operator scanning top-down sees the hub before the spokes.
    result.sort(key=lambda r: (r["server_id"] != "france-panel", r["server_id"]))
    return result


@router.get("/servers/{server_id}/history", dependencies=[Depends(require_auth)])
def history(
    server_id: str,
    hours: int = Query(default=24, ge=1, le=MAX_HISTORY_HOURS),
    session: Session = Depends(get_session),
):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = session.exec(
        select(ServerMetric)
        .where(ServerMetric.server_id == server_id, ServerMetric.ts >= since)
        .order_by(ServerMetric.ts.asc())  # type: ignore[attr-defined]
    ).all()
    return [
        {**r.model_dump(exclude={"extra", "id"}), "extra": json.loads(r.extra or "{}")}
        for r in rows
    ]


@router.get("/events", dependencies=[Depends(require_auth)])
def events(
    limit: int = Query(default=100, ge=1, le=500),
    server_id: Optional[str] = Query(default=None),
    severity: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
):
    stmt = select(MonitorEvent).order_by(MonitorEvent.ts.desc(), MonitorEvent.id.desc())  # type: ignore[attr-defined]
    if server_id:
        stmt = stmt.where(MonitorEvent.server_id == server_id)
    if severity:
        stmt = stmt.where(MonitorEvent.severity == severity)
    rows = session.exec(stmt.limit(limit)).all()
    return [r.model_dump(exclude={"id"}) for r in rows]
