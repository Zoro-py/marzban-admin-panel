"""The finance endpoint's optional since/until window (D22, 2026-09-27).

Unparameterized calls must keep the EXACT old shape — last 30 days of day
buckets, last 30 transactions — because the dashboard is the only caller
today. With a window: day buckets span exactly the window, transactions are
the entries inside it (a date-only `until` covers its whole day, same
face-value contract as /api/history/charges), and the point-in-time cards
are untouched by the window. Bad input (one-sided window, non-YMD, inverted
range) is a 422, not a silent default.

Plain `python -m tests.test_finance_range` from `backend/` — no pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="finance_range_test_")) / "test.db"
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
from app.models import Customer, LedgerEntry, LedgerType  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)

with Session(engine) as session:
    cust = Customer(name="Range Test Cust")
    session.add(cust)
    session.commit()
    session.refresh(cust)
    # Three entries in three different months, at mid-day so a date-only
    # boundary can't hide them in midnight slivers.
    for day, kind, amount in ((datetime(2026, 7, 5, 12, 0), LedgerType.charge, 100.0),
                              (datetime(2026, 8, 20, 12, 0), LedgerType.credit, 50.0),
                              (datetime(2026, 9, 10, 12, 0), LedgerType.charge, 70.0)):
        session.add(LedgerEntry(date=day, type=kind, amount=amount, customer_id=cust.id, source="web"))
    session.commit()

# 1. No params: the old shape — all three transactions visible, buckets cover
#    the last 30 days.
r = client.get("/api/reports/finance")
check("default call is 200", r.status_code == 200, str(r.status_code))
d = r.json()
check("default keeps all transactions", len(d["recent_transactions"]) == 3)
today = datetime.utcnow().date()
bucket_days = [row["date"] for row in d["revenue_by_day"]]
check("default buckets span last 30 days",
      len(bucket_days) == 30 and bucket_days[0] == (today - timedelta(days=29)).isoformat()
      and bucket_days[-1] == today.isoformat(),
      f"n={len(bucket_days)} first={bucket_days[0] if bucket_days else None} last={bucket_days[-1] if bucket_days else None}")

# 2. August window: exactly the August credit; date-only `until` covers the
#    whole 31st.
r = client.get("/api/reports/finance?since=2026-08-01&until=2026-08-31")
check("windowed call is 200", r.status_code == 200, str(r.status_code))
d = r.json()
check("window filters transactions", len(d["recent_transactions"]) == 1
      and d["recent_transactions"][0]["amount"] == 50.0)
check("window buckets only August", [row["date"] for row in d["revenue_by_day"]][0] == "2026-08-01"
      and [row["date"] for row in d["revenue_by_day"]][-1] == "2026-08-31")
check("windowed card totals stay point-in-time",
      isinstance(d["total_outstanding"], (int, float)) and "revenue_this_month" in d)

# 3. Bad windows are 422, not silent defaults.
for label, qs in (
    ("one-sided window", "?since=2026-08-01"),
    ("non-YMD input", "?since=2026/08/01&until=2026-08-31"),
    ("inverted range", "?since=2026-08-31&until=2026-08-01"),
):
    r = client.get(f"/api/reports/finance{qs}")
    check(f"{label} is 422", r.status_code == 422, str(r.status_code))

if failures:
    print(f"\nFAILED: {len(failures)}: {failures}")
    sys.exit(1)
print("\nall finance-window checks passed")
