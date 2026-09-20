"""mark-paid must credit exactly once — even under concurrent double-clicks.

MonthlySettlementBatch.mark_batch_paid checks `marked_paid_at` then posts a
credit then commits. As a sync `def` endpoint it ran in FastAPI's threadpool,
where two concurrent requests could both pass the check and both post a
credit — inventing money in an append-only ledger with no discrepancy
anywhere to notice. It now runs under serialise_billing (the same in-process
billing lock every other money endpoint takes).

Fires two truly concurrent POSTs (one event loop, ASGI transport) against a
seeded batch and requires exactly one credit row. Plain
`python -m tests.test_mark_paid_once` from `backend/` — no pytest, same
harness shape as the rest of this directory.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="mark_paid_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    BillingMode,
    Customer,
    LedgerEntry,
    MonthlySettlementBatch,
)
from sqlmodel import Session, select  # noqa: E402

init_db()

app.dependency_overrides[require_auth] = lambda: "test-admin"

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


_seed_n = [0]


def seed() -> int:
    _seed_n[0] += 1
    n = _seed_n[0]
    with Session(engine) as s:
        cust = Customer(name=f"markpaid-test-{n}")
        s.add(cust)
        s.flush()
        acc = Account(marzban_username=f"markpaid_acc_{n}",
                      customer_id=cust.id, billing_mode=BillingMode.payg)
        s.add(acc)
        s.flush()
        s.add(LedgerEntry(type="charge", amount=100000.0, customer_id=cust.id,
                          account_id=acc.id, note="monthly charge", source="sync"))
        batch = MonthlySettlementBatch(jalali_period="1405-06", account_id=acc.id,
                                       display_name="markpaid-test", billable_gb=20.0,
                                       amount=100000.0, settled_at=datetime.now(timezone.utc))
        s.add(batch)
        s.commit()
        return batch.id


def main() -> None:
    batch_id = seed()
    client = TestClient(app)

    r1 = client.post(f"/api/payg-monthly/batches/{batch_id}/mark-paid")
    r2 = client.post(f"/api/payg-monthly/batches/{batch_id}/mark-paid")
    check("first mark succeeded", r1.status_code in (200, 201), str(r1.status_code))
    check("second sequential mark refused", r2.status_code in (400, 404, 409), str(r2.status_code))

    with Session(engine) as s:
        credits = s.exec(select(LedgerEntry).where(LedgerEntry.type == "credit")).all()
        check("exactly ONE credit row exists (no invented money)", len(credits) == 1,
              f"count={len(credits)} amounts={[c.amount for c in credits]}")
        if credits:
            check("credit amount == batch amount", abs(credits[0].amount - 100000.0) < 0.01)

    print()
    if failures:
        print(f"RESULT: {len(failures)} FAILURES: {failures}")
        sys.exit(1)
    print("RESULT: sequential checks OK")


async def concurrent_double_mark() -> None:
    """Two truly concurrent marks (one event loop, ASGI transport) — the
    billing lock must let exactly one credit through."""
    import httpx

    batch_id = seed()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:

        async def hit():
            return await c.post(f"/api/payg-monthly/batches/{batch_id}/mark-paid")

        r1, r2 = await asyncio.gather(hit(), hit())

    codes = sorted([r1.status_code, r2.status_code])
    successes = sum(1 for c in codes if c in (200, 201))
    check("concurrent: exactly one succeeded, the other refused",
          successes == 1 and all(c in (200, 201, 400, 404, 409) for c in codes),
          f"codes={codes}")
    with Session(engine) as s:
        # one credit from the sequential part + exactly one from this batch
        credits = s.exec(select(LedgerEntry).where(LedgerEntry.type == "credit")).all()
        check("concurrent: one NEW credit only (2 total across both batches)", len(credits) == 2,
              f"count={len(credits)}")

    print()
    if failures:
        print(f"RESULT (concurrent): {len(failures)} FAILURES: {failures}")
        sys.exit(1)
    print("RESULT: concurrent checks OK")


main()
asyncio.run(concurrent_double_mark())
