"""Checks for db.py's one-time (well, self-limiting-forever) backfill that
un-sticks an account ALREADY caught by the lifetime-traffic-drop bug (see
test_lifetime_traffic_rebaseline.py for the going-forward sync_job.py fix —
this is the other half: an account that's been stuck since BEFORE this fix
existed needs a nudge, since no further drop will ever be detected for it
while first_seen_traffic already sits above lifetime_used_traffic).

Plain `python -m tests.test_lifetime_traffic_backfill` from `backend/`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="lifetime_backfill_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""
os.environ["SHOP_BOT_API_KEY"] = ""
os.environ["DELEGATE_BOT_API_KEY"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app.models import Account, Customer, utcnow  # noqa: E402

init_db()  # first pass: creates tables, nothing to backfill yet

GB = 1024 ** 3
failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


with Session(engine) as session:
    customer = Customer(name="Backfill Test Customer")
    session.add(customer)
    session.commit()
    session.refresh(customer)

    now = utcnow().replace(tzinfo=None)

    # STUCK: baseline (500GB) sits ABOVE the current lifetime counter
    # (3GB) — exactly the reported bug's signature, as if this row existed
    # before the sync_job.py fix ever ran.
    stuck = Account(
        marzban_username="stuck-account", customer_id=customer.id,
        lifetime_used_traffic=int(3 * GB), first_seen_traffic=int(500 * GB),
        first_seen_traffic_at=now - timedelta(days=100),
    )
    # HEALTHY: baseline below the counter — normal, must be left alone.
    healthy = Account(
        marzban_username="healthy-account", customer_id=customer.id,
        lifetime_used_traffic=int(10 * GB), first_seen_traffic=int(2 * GB),
        first_seen_traffic_at=now - timedelta(days=10),
    )
    # EQUAL: baseline exactly equals the counter (freshly created, zero
    # usage yet) — must also be left alone, not treated as "stuck".
    fresh = Account(
        marzban_username="fresh-account", customer_id=customer.id,
        lifetime_used_traffic=0, first_seen_traffic=0,
        first_seen_traffic_at=now,
    )
    session.add(stuck)
    session.add(healthy)
    session.add(fresh)
    session.commit()
    session.refresh(stuck)
    session.refresh(healthy)
    session.refresh(fresh)
    stuck_id, healthy_id, fresh_id = stuck.id, healthy.id, fresh.id
    healthy_first_seen_at = healthy.first_seen_traffic_at
    fresh_first_seen_at = fresh.first_seen_traffic_at

# Re-running init_db() is exactly what happens on every real app restart —
# this is the actual code path the fix lives in.
init_db()

with Session(engine) as session:
    acc = session.get(Account, stuck_id)
    check("stuck account: first_seen_traffic re-baselined to the current lifetime value",
          acc.first_seen_traffic == int(3 * GB))
    check("stuck account: first_seen_traffic_at moved forward to (approximately) now",
          (utcnow().replace(tzinfo=None) - acc.first_seen_traffic_at).total_seconds() < 60)

    acc = session.get(Account, healthy_id)
    check("healthy account (baseline already below counter): left untouched",
          acc.first_seen_traffic == int(2 * GB) and acc.first_seen_traffic_at == healthy_first_seen_at)

    acc = session.get(Account, fresh_id)
    check("fresh account (baseline == counter, not '>'): left untouched, not treated as stuck",
          acc.first_seen_traffic == 0 and acc.first_seen_traffic_at == fresh_first_seen_at)

# Running init_db() AGAIN (a second restart) must be a no-op for the
# already-fixed account — self-limiting by construction (no _migration_marker
# needed: the WHERE clause itself stops matching once fixed).
with Session(engine) as session:
    acc = session.get(Account, stuck_id)
    after_first_fix = acc.first_seen_traffic_at

init_db()

with Session(engine) as session:
    acc = session.get(Account, stuck_id)
    check("a second restart does not re-touch an already-fixed account",
          acc.first_seen_traffic_at == after_first_fix)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All lifetime-traffic backfill cases passed.")
