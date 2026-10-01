"""Migration test for the delegate invite-link change (2026-10-01): a
database that still has the OLD delegate table (telegram_id declared NOT
NULL, no claim columns) must survive init_db() — rows copied through the
table rebuild, telegram_id nullable afterwards, and the guarded migration
must be a no-op on the second run.

The old-shape table is created by hand with raw SQL (exactly what the
pre-change model produced on a deployed DB), seeded, and only then is
init_db() called — create_all skips the existing table, leaving
_run_lightweight_migrations to do the work.

Plain `python -m tests.test_delegate_migration` from `backend/`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# DATABASE_URL must be set BEFORE app.db is imported (the engine is created
# at module import) — same bootstrap order as tests/test_delegate_smoke.py.
_TMP_DB = Path(tempfile.mkdtemp(prefix="delegate_migration_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""
os.environ["SHOP_BOT_API_KEY"] = ""
os.environ["DELEGATE_BOT_API_KEY"] = "test-delegate-key"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app.models import Delegate  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


OLD_DELEGATE_DDL = """
CREATE TABLE delegate (
    id INTEGER NOT NULL PRIMARY KEY,
    customer_id INTEGER,
    group_id INTEGER,
    telegram_id INTEGER NOT NULL,
    label VARCHAR,
    is_active BOOLEAN NOT NULL DEFAULT 1,
    credit_limit FLOAT,
    daily_create_cap INTEGER NOT NULL DEFAULT 20,
    username_prefix VARCHAR NOT NULL DEFAULT 'd',
    default_duration_days INTEGER NOT NULL DEFAULT 30,
    created_at DATETIME NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customer (id),
    FOREIGN KEY(group_id) REFERENCES "group" (id)
)
"""

with engine.begin() as conn:
    conn.execute(text(OLD_DELEGATE_DDL))
    conn.execute(text("CREATE UNIQUE INDEX ix_delegate_telegram_id ON delegate (telegram_id)"))
    conn.execute(text("CREATE INDEX ix_delegate_customer_id ON delegate (customer_id)"))
    conn.execute(text("CREATE INDEX ix_delegate_group_id ON delegate (group_id)"))
    # Two real claimed rows (the only state the old schema could represent).
    conn.execute(text(
        "INSERT INTO delegate (customer_id, group_id, telegram_id, label, is_active, "
        "credit_limit, daily_create_cap, username_prefix, default_duration_days, created_at) "
        "VALUES (1, NULL, 9001, 'old ali', 1, 5000.0, 20, 'd', 30, '2026-09-01 10:00:00.000000')"
    ))
    conn.execute(text(
        "INSERT INTO delegate (customer_id, group_id, telegram_id, label, is_active, "
        "credit_limit, daily_create_cap, username_prefix, default_duration_days, created_at) "
        "VALUES (NULL, 5, 9002, NULL, 0, NULL, 15, 'x', 45, '2026-09-02 11:30:00.000000')"
    ))


def delegate_pragma(conn):
    return {row[1]: row for row in conn.execute(text("PRAGMA table_info(delegate)"))}


with engine.begin() as conn:
    before = delegate_pragma(conn)
    check("old table really has telegram_id NOT NULL before the migration", before["telegram_id"][3] == 1)
    check("old table has no claim_token column", "claim_token" not in before)

# create_all creates everything EXCEPT delegate (already exists); the
# lightweight migration then adds the claim columns and rebuilds the table.
init_db()

with engine.begin() as conn:
    after = delegate_pragma(conn)
    check("telegram_id became nullable after init_db()", after["telegram_id"][3] == 0)
    check("claim_token column added", "claim_token" in after)
    check("claim_expires_at column added", "claim_expires_at" in after)
    rows = conn.execute(text(
        "SELECT customer_id, group_id, telegram_id, label, is_active, credit_limit, "
        "daily_create_cap, username_prefix, default_duration_days, created_at "
        "FROM delegate ORDER BY id"
    )).fetchall()
    check("both pre-existing rows survived the rebuild", len(rows) == 2)
    first, second = rows
    check("row 1 copied field-for-field",
          first == (1, None, 9001, "old ali", 1, 5000.0, 20, "d", 30, "2026-09-01 10:00:00.000000"))
    check("row 2 copied field-for-field (inactive group delegate, NULLs preserved)",
          second == (None, 5, 9002, None, 0, None, 15, "x", 45, "2026-09-02 11:30:00.000000"))
    indexes = {row[1] for row in conn.execute(text("PRAGMA index_list(delegate)"))}
    check("the unique telegram_id index was recreated after the rebuild", "ix_delegate_telegram_id" in indexes)
    check("the claim_token index exists", "ix_delegate_claim_token" in indexes)

# Guard must be one-shot: a second init_db() re-reads the PRAGMA, sees
# telegram_id already nullable, and does nothing — rows still intact.
init_db()
with engine.begin() as conn:
    rows = conn.execute(text("SELECT COUNT(*) FROM delegate")).fetchone()
    after2 = delegate_pragma(conn)
    check("second init_db() is a no-op (rows untouched)", rows[0] == 2 and after2["telegram_id"][3] == 0)

# The real point of the nullable flip: the ORM can now insert a PENDING
# invite row (telegram_id NULL) through the model itself.
with Session(engine) as session:
    session.add(Delegate(customer_id=1, telegram_id=None, is_active=False,
                         claim_token="pending-token",
                         claim_expires_at=datetime.now(timezone.utc) + timedelta(days=7)))
    session.commit()
    pending = session.exec(select(Delegate).where(Delegate.claim_token == "pending-token")).first()
    check("a PENDING invite row (telegram_id NULL) inserts cleanly through the ORM",
          pending is not None and pending.telegram_id is None)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All delegate migration cases passed.")
