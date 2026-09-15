"""Checks for GET /api/ledger/balance's new `since` param — "what do they
owe FROM this date forward" (e.g. their last payment date), not all-time.

Plain `python -m tests.test_balance_since` from `backend/`, same harness
shape as tests/test_delegate_smoke.py.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="balance_since_test_")) / "test.db"
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

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Customer, Group, LedgerEntry, LedgerType, utcnow  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)

now = utcnow()
long_ago = now - timedelta(days=90)
last_payment = now - timedelta(days=10)
recent = now - timedelta(days=3)

with Session(engine) as session:
    customer = Customer(name="Since Test Customer")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    customer_id = customer.id

    # Old debt, long since paid off (a credit right after it).
    session.add(LedgerEntry(type=LedgerType.charge, amount=100_000, customer_id=customer_id, date=long_ago))
    session.add(LedgerEntry(type=LedgerType.credit, amount=100_000, customer_id=customer_id, date=long_ago + timedelta(hours=1)))
    # The customer's last payment (this is the date they'd type in).
    session.add(LedgerEntry(type=LedgerType.credit, amount=0, customer_id=customer_id, date=last_payment))
    # New debt accrued since that payment.
    session.add(LedgerEntry(type=LedgerType.charge, amount=50_000, customer_id=customer_id, date=recent))
    session.commit()

# ---- all-time balance: old debt washes out (paid off), net is just the new 50k ----
r = client.get("/api/ledger/balance", params={"customer_id": customer_id})
check("all-time balance is just the new charge (old debt was paid off)", r.json()["balance"] == 50_000.0)

# ---- balance since the last payment date: same 50k (nothing else happened since) ----
since_str = last_payment.replace(tzinfo=timezone.utc).isoformat()
r = client.get("/api/ledger/balance", params={"customer_id": customer_id, "since": since_str})
check("balance since last payment matches (only the new charge counts)", r.json()["balance"] == 50_000.0)

# ---- balance since something AFTER the new charge: 0, nothing posted since then ----
since_after_all = (now + timedelta(days=1)).replace(tzinfo=timezone.utc).isoformat()
r = client.get("/api/ledger/balance", params={"customer_id": customer_id, "since": since_after_all})
check("balance since a future date is 0 (nothing posted yet)", r.json()["balance"] == 0.0)

# ---- balance since long ago (before everything): matches all-time ----
since_before_all = (long_ago - timedelta(days=1)).replace(tzinfo=timezone.utc).isoformat()
r = client.get("/api/ledger/balance", params={"customer_id": customer_id, "since": since_before_all})
check("balance since before everything matches all-time balance", r.json()["balance"] == 50_000.0)

# ---- a date-only string (no time) is accepted and parsed as midnight that day ----
date_only = recent.date().isoformat()
r = client.get("/api/ledger/balance", params={"customer_id": customer_id, "since": date_only})
check("a plain date (no time) is accepted", r.status_code == 200)
check("date-only 'since' still includes same-day-or-later entries", r.json()["balance"] == 50_000.0)

# ---- group balance also respects since (same code path, different scope) ----
with Session(engine) as session:
    group_rep = Customer(name="Since Group Rep")
    session.add(group_rep)
    session.commit()
    session.refresh(group_rep)
    group = Group(name="Since Test Group", representative_customer_id=group_rep.id)
    session.add(group)
    session.commit()
    session.refresh(group)
    group_id = group.id
    session.add(LedgerEntry(type=LedgerType.charge, amount=20_000, group_id=group_id, date=long_ago))
    session.add(LedgerEntry(type=LedgerType.charge, amount=30_000, group_id=group_id, date=recent))
    session.commit()
r = client.get("/api/ledger/balance", params={"group_id": group_id, "since": last_payment.isoformat()})
check("group balance since a date only counts entries after it", r.json()["balance"] == 30_000.0)
r = client.get("/api/ledger/balance", params={"group_id": group_id})
check("group balance without since is the full 50k", r.json()["balance"] == 50_000.0)

# ---- no since param: unaffected, still works exactly as before ----
r = client.get("/api/ledger/balance", params={"customer_id": customer_id})
check("omitting since entirely still works (backward compatible)", r.status_code == 200)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All balance-since-date cases passed.")
