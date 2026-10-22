"""Read-only traffic-history chart data for the subscription page.

Public and unauthenticated like Marzban's own `/sub/{token}/*` endpoints -
the subscription token IS the credential, same trust boundary the page
itself already relies on. Verifies the token the exact way Marzban does
(app/utils/jwt.py: create_subscription_token) so this never has to trust a
value it can't check.

Data source: Marzban's own `node_user_usages` table (read-only mount, same
file and same reasoning as marzban_usage_log.py). That table is CLEARED on
every usage reset - this panel resets on every package activation and payg
settle - so it only ever covers "since the last reset", never a fixed
calendar window. This endpoint reports the real covered span (`since`) and
the frontend hides any range tab that would extend past it, rather than
drawing a chart that implies data which isn't there.
"""

from __future__ import annotations

import base64
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app.config import settings

router = APIRouter(prefix="/api/sub-stats", tags=["sub-stats"])

_RANGES = {
    "24h": (timedelta(hours=24), "hour"),
    "7d": (timedelta(days=7), "day"),
    "30d": (timedelta(days=30), "day"),
    "90d": (timedelta(days=90), "day"),
}


def _connect_ro() -> sqlite3.Connection:
    path = settings.marzban_db_path
    if not path or not os.path.exists(path):
        raise HTTPException(503, "usage history unavailable")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def _verify_token(con: sqlite3.Connection, token: str) -> str:
    """Returns the username, or raises 404 - mirrors Marzban's own check
    (get_subscription_payload) so a token this rejects is one Marzban
    would reject too, and vice versa."""
    secret = con.execute("SELECT secret_key FROM jwt LIMIT 1").fetchone()
    if not secret:
        raise HTTPException(503, "usage history unavailable")
    secret_key = secret[0]

    body, sig = token[:-10], token[-10:]
    # sha256 digest, base64url-encoded, first 10 chars - matches
    # app/utils/jwt.py's create_subscription_token exactly.
    expected_sig = base64.urlsafe_b64encode(sha256((body + secret_key).encode()).digest()).decode()[:10]
    if sig != expected_sig:
        raise HTTPException(404, "not found")

    try:
        padded = body + "=" * (-len(body) % 4)
        raw = base64.urlsafe_b64decode(padded).decode("utf-8")
        username = raw.split(",")[0]
    except Exception:
        raise HTTPException(404, "not found")
    if not username:
        raise HTTPException(404, "not found")
    return username


@router.get("/{token}")
def get_usage_history(token: str, range: str = Query("7d")):
    if range not in _RANGES:
        raise HTTPException(400, "bad range")
    window, bucket = _RANGES[range]

    con = _connect_ro()
    try:
        username = _verify_token(con, token)
        user_row = con.execute(
            "SELECT id, created_at FROM users WHERE username = ?", (username,)
        ).fetchone()
        if not user_row:
            raise HTTPException(404, "not found")
        user_id, created_at = user_row

        earliest_row = con.execute(
            "SELECT MIN(created_at) FROM node_user_usages WHERE user_id = ?", (user_id,)
        ).fetchone()
        earliest = earliest_row[0] if earliest_row else None

        now = datetime.now(timezone.utc)
        start = now - window
        fmt = "%Y-%m-%d %H:00:00" if bucket == "hour" else "%Y-%m-%d 00:00:00"

        rows = con.execute(
            "SELECT created_at, used_traffic FROM node_user_usages "
            "WHERE user_id = ? AND created_at >= ? ORDER BY created_at",
            (user_id, start.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchall()
    finally:
        con.close()

    buckets: dict[str, int] = {}
    for created_at, used in rows:
        try:
            dt = datetime.fromisoformat(str(created_at).replace("T", " ")[:19])
        except ValueError:
            continue
        key = dt.strftime(fmt)
        buckets[key] = buckets.get(key, 0) + int(used or 0)

    points = [{"t": k, "bytes": v} for k, v in sorted(buckets.items())]
    return JSONResponse(
        {"points": points, "since": earliest, "bucket": bucket},
        headers={"Access-Control-Allow-Origin": "*"},
    )
