"""Off-server database backup: safely copies the live SQLite database, zips
it, and pushes it straight to Telegram — so a server that disappears doesn't
also mean this panel's billing data disappears with it. Same idea as
Marzban's own scheduled backup, delivered the same way (a zip in the admin's
chat), run on its own schedule via the AsyncIOScheduler in main.py.
"""

import logging
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Telegram's own limit for a bot-uploaded document.
TELEGRAM_MAX_FILE_BYTES = 45 * 1024 * 1024

JWT_SECRET_PATH = Path(__file__).resolve().parent.parent / ".jwt_secret"


def _sqlite_path_from_url(database_url: str) -> str | None:
    """Extracts the filesystem path from a sqlite:/// URL. Returns None for
    anything else (e.g. postgresql://) — that needs its own backup tool
    (pg_dump), not this one."""
    if not database_url.startswith("sqlite"):
        return None
    return database_url.split("///", 1)[1]


def _safe_copy_sqlite(src_path: str, dst_path: str) -> None:
    """SQLite's own online backup API, not a raw file copy. db.py runs this
    database in WAL mode, where committed data can still be sitting in the
    -wal file rather than the main file — a plain copy can miss it, or catch
    the main file mid-write. The backup API is safe against a live,
    concurrently-written database; a raw copy is not."""
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


async def run_backup() -> dict:
    """Copies the database, zips it, and sends it to BOT_ADMIN_CHAT_ID.
    Raises on any failure rather than swallowing it — a backup that fails
    silently is worse than no backup, since it creates false confidence that
    a restore point exists when it doesn't. The caller (the scheduler, or
    the on-demand endpoint/bot command) is responsible for making that
    failure visible."""
    if not settings.bot_token or not settings.bot_admin_chat_id:
        raise RuntimeError(
            "BOT_TOKEN / BOT_ADMIN_CHAT_ID not set in backend/.env — nowhere to send the backup"
        )

    db_path = _sqlite_path_from_url(settings.database_url)
    if db_path is None:
        raise RuntimeError(
            f"No SQLite backup path for DATABASE_URL={settings.database_url!r} — "
            "back this database up with its own tooling instead"
        )
    if not os.path.exists(db_path):
        raise RuntimeError(f"Database file not found at {db_path}")

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d_%H%M")

    with tempfile.TemporaryDirectory() as tmp:
        db_copy_path = os.path.join(tmp, "vpn.db")
        _safe_copy_sqlite(db_path, db_copy_path)

        zip_path = os.path.join(tmp, f"vpn-backup-{stamp}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_copy_path, arcname="vpn.db")
            # Bundled so restoring onto a fresh server keeps existing JWTs
            # valid (see db.py's _load_or_create_jwt_secret) instead of
            # silently logging every session out on top of everything else.
            if JWT_SECRET_PATH.exists():
                zf.write(JWT_SECRET_PATH, arcname=".jwt_secret")

        size = os.path.getsize(zip_path)
        if size > TELEGRAM_MAX_FILE_BYTES:
            raise RuntimeError(
                f"Backup zip is {size / 1024 / 1024:.1f} MB, over Telegram's "
                f"{TELEGRAM_MAX_FILE_BYTES / 1024 / 1024:.0f} MB bot upload limit — back up manually instead"
            )

        caption = f"VPN panel backup — {now.strftime('%Y-%m-%d %H:%M')} UTC"
        async with httpx.AsyncClient(timeout=60) as client:
            with open(zip_path, "rb") as f:
                resp = await client.post(
                    f"https://api.telegram.org/bot{settings.bot_token}/sendDocument",
                    data={"chat_id": settings.bot_admin_chat_id, "caption": caption},
                    files={"document": (os.path.basename(zip_path), f, "application/zip")},
                )
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram rejected the backup upload ({resp.status_code}): {resp.text}")

        logger.info("Backup sent to Telegram: %s (%.1f KB)", os.path.basename(zip_path), size / 1024)
        return {"sent_at": now, "filename": os.path.basename(zip_path), "size_bytes": size}
