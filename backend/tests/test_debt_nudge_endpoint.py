"""Checks for POST /api/notifications/debt-nudge/run — the manual twin of
the every-other-day scheduled debt nudge. Same harness shape as
tests/test_balance_since.py: plain `python -m tests.test_debt_nudge_endpoint`
from `backend/`. The Telegram send is captured, never hit.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="nudge_endpoint_test_")) / "test.db"
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

import app.debt_nudge_job as debt_nudge_job  # noqa: E402
from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Customer, LedgerEntry, LedgerType, utcnow  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)

# Capture Telegram sends (with their keyboards) instead of hitting the API —
# the nudge must never need real credentials to be testable.
sent_messages: list[tuple[str, dict]] = []


async def _capture_send(text, reply_markup):
    sent_messages.append((text, reply_markup))


debt_nudge_job.notify_admin_with_buttons = _capture_send

now = utcnow().replace(tzinfo=None)

with Session(engine) as session:
    customer = Customer(name="Nudge Endpoint Tester")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    customer_id = customer.id
    # One unpaid charge, old enough (>= 14 days) to be worth a nudge.
    session.add(LedgerEntry(type=LedgerType.charge, amount=120_000, customer_id=customer_id,
                            date=now - timedelta(days=40)))
    session.commit()

# ---- manual run with an eligible debtor: sent, counted, and actually handed to Telegram ----
r = client.post("/api/notifications/debt-nudge/run")
check("manual nudge with an eligible debtor reports sent", r.status_code == 200 and r.json()["sent"] is True)
check("manual nudge counts the one eligible debtor", r.json()["count"] == 1)
check("the message reached the (captured) Telegram send", len(sent_messages) == 1)

text, markup = sent_messages[0]
buttons = [b for row in markup["inline_keyboard"] for b in row]
check("the message is a debt-nudge summary", "بدهی‌های قدیمی" in text)
check("one button carries this debtor's debtnudge callback",
      any(b["callback_data"] == f"debtnudge:{customer_id}" for b in buttons))
check("the debtor's button shows their amount", any("120,000" in b["text"] for b in buttons))

# ---- the read-only preview the Telegram console's list screen re-reads ----
r = client.get("/api/notifications/debt-nudge")
overdue = r.json()["overdue"]
check("preview returns the eligible debtor, oldest first",
      r.status_code == 200 and len(overdue) == 1 and overdue[0]["customer_id"] == customer_id
      and overdue[0]["amount"] == 120_000.0)

# ---- a freshly-charged customer is NOT eligible: the same pass must skip them ----
with Session(engine) as session:
    recent = Customer(name="Nudge Too Recent")
    session.add(recent)
    session.commit()
    session.refresh(recent)
    recent_id = recent.id
    session.add(LedgerEntry(type=LedgerType.charge, amount=50_000, customer_id=recent.id,
                            date=now - timedelta(days=2)))
    session.commit()

r = client.post("/api/notifications/debt-nudge/run")
check("recent debt is skipped by the manual nudge too", r.json()["sent"] is True and r.json()["count"] == 1)
text2, markup2 = sent_messages[1]
callbacks2 = [b["callback_data"] for row in markup2["inline_keyboard"] for b in row]
check("only one more telegram send happened, without the recent debtor",
      len(sent_messages) == 2 and f"debtnudge:{recent_id}" not in callbacks2)

# ---- the quieter second list: owed-but-not-nudge-worthy customers are visible, never nudged ----
with Session(engine) as session:
    fam = Customer(name="Accruing Family", kind="family")
    session.add(fam)
    session.commit()
    session.refresh(fam)
    fam_id = fam.id
    session.add(LedgerEntry(type=LedgerType.charge, amount=80_000, customer_id=fam.id, date=now - timedelta(days=3)))
    session.commit()

body = client.get("/api/notifications/debt-nudge").json()
accruing = {r["customer_id"]: r for r in body["accruing"]}
check("overdue list is unchanged by the accruing addition", [r["customer_id"] for r in body["overdue"]] == [customer_id])
check("a recent debtor shows up under accruing", recent_id in accruing and accruing[recent_id]["amount"] == 50_000.0)
check("accruing rows carry kind", accruing[fam_id]["kind"] == "family" and accruing[recent_id]["kind"] == "individual")
check("an overdue debtor is NOT repeated under accruing", customer_id not in accruing)
check("accruing is largest-first", [r["amount"] for r in body["accruing"]] == sorted((r["amount"] for r in body["accruing"]), reverse=True))
r = client.post("/api/notifications/debt-nudge/run")
callbacks3 = [b["callback_data"] for row in sent_messages[-1][1]["inline_keyboard"] for b in row]
check("the family is still NOT nudged (age rule unchanged)", f"debtnudge:{fam_id}" not in callbacks3)
r = client.get("/api/reports/summary").json()
check("dashboard summary rows carry kind", any(c["customer_id"] == fam_id and c["kind"] == "family" for c in r["overdue_customers"]))

# ---- a Telegram failure surfaces as sent=false + error on a 200, per the job's contract ----


async def _failing_send(text, reply_markup):
    raise RuntimeError("Telegram down")


debt_nudge_job.notify_admin_with_buttons = _failing_send
r = client.post("/api/notifications/debt-nudge/run")
check("a telegram failure returns sent=false with the error, not a 5xx",
      r.status_code == 200 and r.json()["sent"] is False and "Telegram down" in r.json()["error"])

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All debt-nudge endpoint cases passed.")
