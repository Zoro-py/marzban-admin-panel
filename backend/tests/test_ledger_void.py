"""Ledger void / unvoid — the mistaken-deposit feature (2026-10-24).

The property under test: a VOIDED LedgerEntry stops existing as money
everywhere — every balance, sum, statement, finance bucket, aging replay and
audit pass in the system drops it — while the row itself stays in the table
struck through with who/when/why, and un-void puts it back.

Covers every consumer that reads LedgerEntry sums (each was grepped when the
feature landed — this test locks the list so a new sum site that forgets the
`voided_at.is_(None)` filter fails here):

  1. MoneyBook — posted/credits/GB/charge-meta per scope + roll-ups
     (note: customer_posted ROLLS UP account buckets — expectations below
     follow DOMAIN_AND_BILLING.md §1, not the customer-only bucket alone)
  2. account_posted_balance / group_only_posted_balance (settle paths)
  3. attributable_consumed_gb (meter attribution)
  4. GET /api/ledger/statement (charged/paid)
  5. GET /api/reports/finance (month totals, recent list flag)
  6. GET /api/history/charges (entries flagged, summaries/totals live-only)
  7. debt_nudge_job._customer_ledger_scope (debt aging)
  8. POST /api/ledger/{id}/void + /unvoid (state guards, before/after balances,
     mandatory reason, already-voided 409, list keeps showing stamped rows)

Plain `python -m tests.test_ledger_void` from `backend/` — no pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="void_test_")) / "test.db"
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
from sqlmodel import Session, select  # noqa: E402

from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Customer, Group, LedgerEntry, LedgerType  # noqa: E402
from app.services import (  # noqa: E402
    MoneyBook,
    account_posted_balance,
    attributable_consumed_gb,
    group_only_posted_balance,
)

init_db()

failures: list[str] = []


def check(name: str, cond: bool, extra=None):
    print(("  [OK] " if cond else "[FAIL] ") + name + (f"  {extra}" if (extra is not None and not cond) else ""))
    if not cond:
        failures.append(name)


app.dependency_overrides[require_auth] = lambda: "testop"
client = TestClient(app)


def entry(customer_id=None, group_id=None, account_id=None, type="charge", amount=100.0, note=None, gb=None, consumed=None):
    with Session(engine) as s:
        e = LedgerEntry(
            type=type, amount=amount, customer_id=customer_id, group_id=group_id,
            account_id=account_id, note=note, gb_amount=gb, consumed_gb=consumed,
        )
        s.add(e)
        s.commit()
        s.refresh(e)
        return e.id


with Session(engine) as s:
    cust = Customer(name="VoidTest Customer")
    acc = Account(marzban_username="voidtest_acc")
    s.add(cust)
    s.flush()
    acc.customer_id = cust.id
    # A live meter with 15 GB accrued since baseline, so attributable_consumed_gb
    # has something real to attribute (accrued - recorded).
    acc.used_traffic = 15 * 1024**3
    s.add(acc)
    s.commit()
    s.refresh(cust)
    s.refresh(acc)
    CUST_ID, ACC_ID = cust.id, acc.id

# ---- 1) baseline books -----------------------------------------------------
# customer-level charge + credit, two account-level charges (one carries the
# meter-attribution columns).
CH_ID = entry(customer_id=CUST_ID, type="charge", amount=300.0, gb=30.0, note="package sold")
CR_ID = entry(customer_id=CUST_ID, type="credit", amount=100.0, note="payment received")
ACC_CH_ID = entry(account_id=ACC_ID, type="charge", amount=50.0)
ACC_CH2_ID = entry(account_id=ACC_ID, type="charge", amount=20.0, gb=2.0, consumed=5.0)

with Session(engine) as s:
    book = MoneyBook(s)
    cust_obj = s.get(Customer, CUST_ID)
    acc_obj = s.get(Account, ACC_ID)
    check("baseline: customer posted = 270 (300-100 + 50 + 20, roll-up)", book.customer_posted(cust_obj) == 270.0, book.customer_posted(cust_obj))
    check("baseline: account posted = 70", book.account_posted(acc_obj) == 70.0, book.account_posted(acc_obj))
    check("baseline: account_posted_balance = 70", account_posted_balance(s, ACC_ID) == 70.0)
    check("baseline: gb charged = 32 (30 + 2)", book.customer_gb(cust_obj)[0] == 32.0, book.customer_gb(cust_obj))
    check("baseline: charge count = 3", book.customer_charge_meta(cust_obj)[0] == 3, book.customer_charge_meta(cust_obj))
    check("baseline: credits = 100", book.customer_credits(cust_obj) == 100.0)
    check("baseline: meter attribution = 10 (15 accrued - 5 recorded)", attributable_consumed_gb(s, acc_obj) == 10.0, attributable_consumed_gb(s, acc_obj))

# ---- 2) void the credit via the endpoint ----------------------------------
r = client.post(f"/api/ledger/{CR_ID}/void", json={"reason": "اشتباه تایپی — دوبار ثبت شد"})
check("void endpoint: 200", r.status_code == 200, r.text)
body = r.json()
check("void endpoint: stamped by operator", body["entry"]["voided_by"] == "testop")
check("void endpoint: reason stored", body["entry"]["void_reason"] == "اشتباه تایپی — دوبار ثبت شد")
check("void endpoint: balance +100 (credit withdrawn → owes more)", body["scope_balance_after"] - body["scope_balance_before"] == 100.0, body)

with Session(engine) as s:
    book = MoneyBook(s)
    cust_obj = s.get(Customer, CUST_ID)
    check("moneybook: voided credit excluded → posted = 370", book.customer_posted(cust_obj) == 370.0, book.customer_posted(cust_obj))
    check("moneybook: voided credit out of credits", book.customer_credits(cust_obj) in (None, 0.0), book.customer_credits(cust_obj))

r2 = client.post(f"/api/ledger/{CR_ID}/void", json={"reason": "second void attempt"})
check("void endpoint: already-voided → 409", r2.status_code == 409, r2.status_code)
r3 = client.post(f"/api/ledger/{CH_ID}/void", json={"reason": ""})
check("void endpoint: empty reason → 422", r3.status_code == 422, r3.status_code)
r4 = client.post("/api/ledger/999999/void", json={"reason": "no such row"})
check("void endpoint: unknown id → 404", r4.status_code == 404, r4.status_code)

# ---- 3) void a charge: GB + meta + meter attribution ----------------------
r5 = client.post(f"/api/ledger/{ACC_CH2_ID}/void", json={"reason": "بسته اشتباه فروخته شد"})
check("void charge endpoint: 200", r5.status_code == 200, r5.text)
check("void charge: balance -20", r5.json()["scope_balance_after"] - r5.json()["scope_balance_before"] == -20.0)
with Session(engine) as s:
    book = MoneyBook(s)
    cust_obj = s.get(Customer, CUST_ID)
    acc_obj = s.get(Account, ACC_ID)
    check("moneybook: voided charge dropped → posted = 350", book.customer_posted(cust_obj) == 350.0, book.customer_posted(cust_obj))
    check("moneybook: voided GB dropped → 30", book.customer_gb(cust_obj)[0] == 30.0, book.customer_gb(cust_obj))
    check("moneybook: voided charge out of meta", book.customer_charge_meta(cust_obj)[0] == 2, book.customer_charge_meta(cust_obj))
    check("meter attribution: voided consumed_gb excluded → 15 (all accrued, none recorded)", attributable_consumed_gb(s, acc_obj) == 15.0, attributable_consumed_gb(s, acc_obj))

# ---- 4) statement excludes voided -----------------------------------------
r6 = client.get("/api/ledger/statement", params={"customer_id": CUST_ID, "since": "2000-01-01T00:00:00", "usage": "false"})
check("statement: 200", r6.status_code == 200, r6.text)
st = r6.json()
check("statement: charged = 350 (CH 300 + ACC_CH 50 live; voided 20/100 out)", st["statement"]["charged"] == 350.0, st["statement"])
check("statement: paid = 0 (credit voided)", st["statement"]["paid"] == 0.0, st["statement"])

# ---- 5) finance buckets exclude voided; recent list flags them ------------
r7 = client.get("/api/reports/finance")
check("finance: 200", r7.status_code == 200, r7.text)
fin = r7.json()
check("finance: month revenue = 0 (voided credit out)", fin["revenue_this_month"] == 0.0, fin.get("revenue_this_month"))
check("finance: month charged = 350 (voided 20 out)", fin["charged_this_month"] == 350.0, fin.get("charged_this_month"))
voided_rows = [t for t in fin["recent_transactions"] if t.get("voided")]
check("finance: recent list still SHOWS voided rows (flagged)", len(voided_rows) == 2, len(voided_rows))
check("finance: voided rows carry reason", all(t.get("void_reason") for t in voided_rows))

# ---- 6) history charges: flagged entries, live-only summaries -------------
r8 = client.get("/api/history/charges", params={"account_ids": str(ACC_ID), "since": "2000-01-01", "until": "2999-01-01"})
check("history charges: 200", r8.status_code == 200, r8.text)
hist = r8.json()
check("history: voided entry still listed (flagged)", any(e["id"] == ACC_CH2_ID and e.get("voided") for e in hist["entries"]), hist["entries"])
check("history: totals exclude voided (charged=50)", hist["totals"]["charged_amount"] == 50.0, hist["totals"])
check("history: summary GB unknown (only gb-bearing charge was voided)", hist["totals"]["charged_gb_known"] is None, hist["totals"])

# ---- 7) debt-nudge replay drops voided ------------------------------------
from app import debt_nudge_job  # noqa: E402

with Session(engine) as s:
    cust_obj = s.get(Customer, CUST_ID)
    rows = debt_nudge_job._customer_ledger_scope(s, cust_obj)
    check("debt nudge: only live rows in the replay scope (CH + ACC_CH)", [e.id for e in rows] == [CH_ID, ACC_CH_ID], [e.id for e in rows])

# ---- 8) unvoid restores everything ----------------------------------------
r9 = client.post(f"/api/ledger/{ACC_CH2_ID}/unvoid")
check("unvoid endpoint: 200", r9.status_code == 200, r9.text)
check("unvoid endpoint: stamp cleared", r9.json()["entry"]["voided_at"] is None)
r10 = client.post(f"/api/ledger/{ACC_CH2_ID}/unvoid")
check("unvoid endpoint: not-voided → 409", r10.status_code == 409, r10.status_code)
with Session(engine) as s:
    book = MoneyBook(s)
    cust_obj = s.get(Customer, CUST_ID)
    acc_obj = s.get(Account, ACC_ID)
    check("moneybook: unvoided charge back → posted = 370", book.customer_posted(cust_obj) == 370.0, book.customer_posted(cust_obj))
    check("moneybook: unvoided GB back = 32", book.customer_gb(cust_obj)[0] == 32.0, book.customer_gb(cust_obj))
    check("meter attribution: unvoided consumed back = 10", attributable_consumed_gb(s, acc_obj) == 10.0)

# ---- 9) group-only bucket path --------------------------------------------
with Session(engine) as s:
    g = Group(name="VoidTest Group", representative_customer_id=CUST_ID)
    s.add(g)
    s.commit()
    s.refresh(g)
    G_ID = g.id
G_CH = entry(group_id=G_ID, type="charge", amount=77.0)
with Session(engine) as s:
    check("group-only balance before void = 77", group_only_posted_balance(s, G_ID) == 77.0)
rv = client.post(f"/api/ledger/{G_CH}/void", json={"reason": "group charge was a duplicate"})
check("group void: 200", rv.status_code == 200, rv.text)
with Session(engine) as s:
    check("group-only balance after void = 0", group_only_posted_balance(s, G_ID) == 0.0, group_only_posted_balance(s, G_ID))

# ledger LIST still returns voided rows (visibility surface)
r11 = client.get("/api/ledger", params={"customer_id": CUST_ID})
listed = r11.json()
check("ledger list: voided rows still listed (with stamp)", any(e["id"] == CR_ID and e["voided_at"] for e in listed), len(listed))

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All ledger-void cases passed.")
