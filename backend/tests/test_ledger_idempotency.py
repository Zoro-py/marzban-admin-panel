"""POST /api/ledger with an `Idempotency-Key`: same key = one entry, however
many concurrent/repeated requests; no key = unchanged behavior (every request
appends, so a legitimately repeated identical entry is never blocked).

Plain `python tests/test_ledger_idempotency.py` from `backend/`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="idem_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Customer, LedgerEntry  # noqa: E402

init_db()
app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


with Session(engine) as s:
    c = Customer(name="Idem Tester")
    s.add(c)
    s.commit()
    s.refresh(c)
    cid = c.id


def rows() -> int:
    with Session(engine) as s:
        return len(s.exec(select(LedgerEntry).where(LedgerEntry.customer_id == cid)).all())


body = {"type": "credit", "amount": 50000, "customer_id": cid, "note": "payment"}

r1 = client.post("/api/ledger", json=body, headers={"Idempotency-Key": "k-1"})
r2 = client.post("/api/ledger", json=body, headers={"Idempotency-Key": "k-1"})
check("same key twice: both 200", r1.status_code == 200 and r2.status_code == 200)
check("same key twice: the SAME entry comes back", r1.json()["id"] == r2.json()["id"])
check("same key twice: one row written", rows() == 1)

with ThreadPoolExecutor(8) as ex:
    rs = list(ex.map(lambda _: client.post("/api/ledger", json=body, headers={"Idempotency-Key": "k-race"}), range(8)))
check("8 concurrent posts with one key: all 200", all(r.status_code == 200 for r in rs))
check("8 concurrent posts with one key: exactly one new row", rows() == 2, f"rows={rows()}")
check("8 concurrent posts with one key: one entry id", len({r.json()["id"] for r in rs}) == 1)

r3 = client.post("/api/ledger", json=body, headers={"Idempotency-Key": "k-2"})
check("a different key is a genuinely new entry", r3.status_code == 200 and rows() == 3)

client.post("/api/ledger", json=body)
client.post("/api/ledger", json=body)
check("no key: behavior unchanged, every request appends", rows() == 5)

bad = client.post("/api/ledger", json={**body, "amount": -1}, headers={"Idempotency-Key": "k-bad"})
ok_after = client.post("/api/ledger", json=body, headers={"Idempotency-Key": "k-bad"})
check("a rejected request does not burn its key", bad.status_code in (400, 422) and ok_after.status_code == 200 and rows() == 6)

clash = client.post("/api/ledger", json={**body, "amount": 99999}, headers={"Idempotency-Key": "k-1"})
check("same key with a DIFFERENT payload is refused (409), not answered with the old entry", clash.status_code == 409 and rows() == 6)

long = client.post("/api/ledger", json=body, headers={"Idempotency-Key": "x" * 200})
check("absurdly long key is refused (422)", long.status_code == 422)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All ledger idempotency cases passed.")
