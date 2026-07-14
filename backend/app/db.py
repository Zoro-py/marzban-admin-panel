from datetime import datetime, timezone

from sqlalchemy import text
from sqlmodel import SQLModel, Session, create_engine

from app.config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, echo=False, connect_args=connect_args)


def _run_lightweight_migrations() -> None:
    """No formal migration framework for a project this size — `create_all` only
    creates tables that don't exist yet, so a column added to models.py after a
    table already exists on a deployed server needs to be patched in by hand.
    Safe to run on every startup: each check is a no-op once the column exists."""
    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(account)"))}
        if existing and "billing_mode" not in existing:
            conn.execute(text("ALTER TABLE account ADD COLUMN billing_mode VARCHAR NOT NULL DEFAULT 'prepay'"))

        if existing and "first_seen_traffic" not in existing:
            conn.execute(text("ALTER TABLE account ADD COLUMN first_seen_traffic INTEGER NOT NULL DEFAULT 0"))
            conn.execute(text("ALTER TABLE account ADD COLUMN first_seen_traffic_at DATETIME"))
            # Backfill: for accounts that already existed before this column did, we
            # have no record of their traffic at "first seen" — the safe choice is to
            # start the observation window NOW (baseline = current lifetime total),
            # not to guess. This makes every pre-existing account correctly report
            # "insufficient data" until it accumulates a few real days of *observed*
            # growth, rather than resurrecting the exact misattribution bug this
            # column exists to prevent.
            now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
            conn.execute(
                text(
                    "UPDATE account SET first_seen_traffic = lifetime_used_traffic, "
                    "first_seen_traffic_at = :now WHERE first_seen_traffic_at IS NULL"
                ),
                {"now": now},
            )


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    if settings.database_url.startswith("sqlite"):
        _run_lightweight_migrations()


def get_session():
    with Session(engine) as session:
        yield session
