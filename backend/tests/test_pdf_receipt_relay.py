"""PDF receipts reach the operator as a DOCUMENT (2026-11-18).

relay_shop_photo_to_admin already downloads the receipt's bytes with the
SHOP bot's token (a file_id can't cross bots) — but the upload was
hardcoded to sendPhoto, so a bank-app PDF export could never reach the
operator's decision screen and the customer was bounced at the worst
moment. The relay now picks sendDocument for a PDF — detected from
Telegram's file_path extension or the bytes' own %PDF- magic, so no schema
change and no new field: the bot passes the same receipt_file_id through.

Plain `python -m tests.test_pdf_receipt_relay` from `backend/`.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="pdf_relay_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = "123:op"
os.environ["BOT_ADMIN_CHAT_ID"] = "1"
os.environ["SHOP_BOT_TOKEN"] = "456:shop"
os.environ["SHOP_BOT_API_KEY"] = ""
os.environ["DELEGATE_BOT_API_KEY"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import notify as notify_mod  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class _StubResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None, content: bytes = b""):
        self.status_code = status_code
        self.text = "stub"
        self.content = content
        self._payload = payload or {}

    def json(self):
        return self._payload


class _StubClient:
    """Replaces httpx.AsyncClient inside notify.py: serves getFile + the file
    download from a canned fixture, records every upload."""

    file_path = "photos/file_1.jpg"
    blob = b"\xff\xd8\xff\xe0 fake-jpeg"
    uploads: list[dict] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        if "/getFile" in url:
            return _StubResponse(200, {"result": {"file_path": _StubClient.file_path}})
        return _StubResponse(200, content=_StubClient.blob)  # the bytes download

    async def post(self, url, data=None, files=None, **k):
        _StubClient.uploads.append({
            "url": url,
            "files": files or {},
            "data": data or {},
        })
        return _StubResponse(200)


notify_mod.httpx.AsyncClient = _StubClient
KEYBOARD = {"inline_keyboard": [[{"text": "✅ Approve", "callback_data": "topup:ok:1"}]]}


async def relay(file_path: str, blob: bytes) -> None:
    _StubClient.file_path = file_path
    _StubClient.blob = blob
    _StubClient.uploads.clear()
    await notify_mod.relay_shop_photo_to_admin("file-id-x", "caption", KEYBOARD)


async def main() -> None:
    await relay("documents/file_5.pdf", b"%PDF-1.4 fake")
    up = _StubClient.uploads[-1]
    check("a .pdf file_path uploads via sendDocument",
          up["url"].endswith("/sendDocument"), repr(up["url"]))
    check("the PDF travels under the document part as application/pdf",
          "document" in up["files"] and up["files"]["document"][2] == "application/pdf",
          repr(list(up["files"])))
    check("the original filename survives (receipt naming)",
          up["files"]["document"][0] == "file_5.pdf", repr(up["files"]["document"][0]))
    check("the decision buttons ride the document message",
          up["data"].get("reply_markup") is not None, repr(up["data"]))

    await relay("documents/file_6.bin", b"%PDF-1.4 magic-only")
    up = _StubClient.uploads[-1]
    check("a PDF by MAGIC BYTES (extensionless path) also goes sendDocument",
          up["url"].endswith("/sendDocument"), repr(up["url"]))

    await relay("photos/file_1.jpg", b"\xff\xd8\xff\xe0 fake-jpeg")
    up = _StubClient.uploads[-1]
    check("an image still goes sendPhoto under the photo part",
          up["url"].endswith("/sendPhoto") and "photo" in up["files"], repr(up["url"]))


asyncio.run(main())

print()
if failures:
    print(f"RESULT: {len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("RESULT: pdf receipt relay checks OK")
