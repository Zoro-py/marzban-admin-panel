"""Read-only access to Marzban's per-user usage RESET LOG.

Why this exists: Marzban's per-node usage history (`node_user_usages`, what
/api/user/{u}/usage returns) is wiped for a user on every usage reset —
crud.reset_user_data_usage does `dbuser.node_usages.clear()` — and this panel
resets usage at every package activation and every payg settle. So that
history only ever covers "since the last reset" and cannot answer how much an
account used across package changes. What Marzban DOES keep at each reset is
`user_usage_logs.used_traffic_at_reset`: the exact traffic of the period that
just ended. Together with the user's live `used_traffic` that is a complete,
exact consumption record per period, back to the user's creation.

Marzban exposes no API for that table, so we read its sqlite file, mounted
READ-ONLY into this container (settings.marzban_db_path). sqlite opened with
mode=ro; Marzban runs in rollback-journal mode, where a reader on a read-only
mount works (verified on the live box, 2026-10-10). Nothing here writes.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.config import settings

log = logging.getLogger(__name__)


@dataclass
class UsagePeriod:
    start: datetime
    end: Optional[datetime]  # None = the current, still-open period
    used_bytes: int


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    return datetime.fromisoformat(str(ts).replace("T", " ").split("+")[0].rstrip("Z"))


def load_periods(usernames: list[str]) -> tuple[Optional[dict[str, list[UsagePeriod]]], Optional[str]]:
    """{username: [UsagePeriod...] oldest first} for every user Marzban knows,
    or (None, reason) when the log can't be read. A user missing from the dict
    is unknown to Marzban (deleted there)."""
    path = settings.marzban_db_path
    if not path:
        return None, "Marzban usage log not configured (MARZBAN_DB_PATH)"
    if not os.path.exists(path):
        return None, "Marzban usage log not mounted"
    if not usernames:
        return {}, None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            marks = ",".join("?" * len(usernames))
            users = con.execute(
                f"SELECT id, username, created_at, used_traffic FROM users WHERE username IN ({marks})", usernames
            ).fetchall()
            by_id = {u[0]: u for u in users}
            resets: dict[int, list[tuple[datetime, int]]] = {uid: [] for uid in by_id}
            if by_id:
                ids = list(by_id)
                rows = con.execute(
                    f"SELECT user_id, reset_at, used_traffic_at_reset FROM user_usage_logs "
                    f"WHERE user_id IN ({','.join('?' * len(ids))}) ORDER BY reset_at", ids
                ).fetchall()
                for uid, at, used in rows:
                    resets[uid].append((_parse(at), int(used or 0)))
        finally:
            con.close()
    except sqlite3.Error as exc:
        log.warning("Marzban usage log read failed: %s", exc)
        return None, f"Marzban usage log unreadable ({type(exc).__name__})"

    out: dict[str, list[UsagePeriod]] = {}
    for uid, (_, username, created_at, used_now) in by_id.items():
        periods: list[UsagePeriod] = []
        start = _parse(created_at)
        for at, used in resets[uid]:
            if start is None:
                start = at
            periods.append(UsagePeriod(start, at, used))
            start = at
        periods.append(UsagePeriod(start or datetime.min, None, int(used_now or 0)))
        out[username] = periods
    return out, None
