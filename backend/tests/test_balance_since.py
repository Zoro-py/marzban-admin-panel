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
from app.models import Account, BillingMode, Customer, Group, LedgerEntry, LedgerType, utcnow  # noqa: E402
from app.services import get_settings  # noqa: E402

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

# ---- account-level balance respects since too (same code path, account scope) ----
with Session(engine) as session:
    acct_customer = Customer(name="Since Account Customer")
    session.add(acct_customer)
    session.commit()
    session.refresh(acct_customer)
    account = Account(marzban_username="since-acct", customer_id=acct_customer.id)
    session.add(account)
    session.commit()
    session.refresh(account)
    account_id = account.id
    session.add(LedgerEntry(type=LedgerType.charge, amount=15_000, account_id=account_id, date=long_ago))
    session.add(LedgerEntry(type=LedgerType.charge, amount=25_000, account_id=account_id, date=recent))
    session.commit()
r = client.get("/api/ledger/balance", params={"account_id": account_id, "since": last_payment.isoformat()})
check("account balance since a date only counts entries after it", r.status_code == 200 and r.json()["balance"] == 25_000.0)
check("account balance response echoes entity_type='account'", r.json()["entity_type"] == "account")
r = client.get("/api/ledger/balance", params={"account_id": account_id})
check("account balance without since is the full 40k", r.json()["balance"] == 40_000.0)

# ---- providing more than one, or none, of customer/group/account is refused ----
r = client.get("/api/ledger/balance", params={"customer_id": customer_id, "account_id": account_id})
check("providing both customer_id and account_id is refused (400)", r.status_code == 400)
r = client.get("/api/ledger/balance")
check("providing none of the three is refused (400)", r.status_code == 400)

# ---- no since param: unaffected, still works exactly as before ----
r = client.get("/api/ledger/balance", params={"customer_id": customer_id})
check("omitting since entirely still works (backward compatible)", r.status_code == 200)

# ---- credited_amount: gross credits in the window, None when no credit rows ----
with Session(engine) as session:
    credit_customer = Customer(name="Since Credit Customer")
    session.add(credit_customer)
    session.commit()
    session.refresh(credit_customer)
    credit_customer_id = credit_customer.id
    # An old paid-off cycle (charge+credit pair, both before every window
    # used below) and a fresh charge with a PARTIAL payment inside it.
    session.add(LedgerEntry(type=LedgerType.charge, amount=70_000, customer_id=credit_customer_id, date=long_ago))
    session.add(LedgerEntry(type=LedgerType.credit, amount=70_000, customer_id=credit_customer_id, date=long_ago + timedelta(hours=1)))
    session.add(LedgerEntry(type=LedgerType.charge, amount=40_000, customer_id=credit_customer_id, date=recent))
    session.add(LedgerEntry(type=LedgerType.credit, amount=15_000, customer_id=credit_customer_id, date=recent + timedelta(hours=2)))
    session.commit()

r = client.get("/api/ledger/balance", params={"customer_id": credit_customer_id})
check("all-time credited_amount is the gross sum of credit rows", r.json()["credited_amount"] == 85_000.0)
check("all-time balance still nets charges against credits", r.json()["balance"] == 25_000.0)
since_before_pair = (recent - timedelta(days=1)).replace(tzinfo=timezone.utc).isoformat()
r = client.get("/api/ledger/balance", params={"customer_id": credit_customer_id, "since": since_before_pair})
check("window credited_amount counts only credits dated in the window", r.json()["credited_amount"] == 15_000.0)
r = client.get("/api/ledger/balance", params={"customer_id": credit_customer_id, "since": since_after_all})
check("a window with no credit rows reports credited_amount null", r.json()["credited_amount"] is None)

# ---- an offset-aware `since` means its UTC instant, not its wall clock ----
# SQLite's driver binds a datetime's own wall-clock fields verbatim, so a
# non-UTC offset would silently compare as that wall time against stored UTC
# rows. The endpoint normalises to UTC first: an entry stamped at `recent`
# must stay inside a window whose offset-spelled since has the same instant,
# even though its wall-clock reading is 3.5h later.
tehran = timezone(timedelta(hours=3, minutes=30))
since_tehran_wall = (recent + timedelta(hours=3, minutes=30)).replace(tzinfo=tehran).isoformat()
r = client.get("/api/ledger/balance", params={"customer_id": customer_id, "since": since_tehran_wall})
check("an offset-aware since is honoured as its UTC instant, not its wall clock", r.json()["balance"] == 50_000.0)
r = client.get("/api/ledger/balance", params={"customer_id": customer_id, "since": recent.replace(tzinfo=timezone.utc).isoformat()})
check("the same instant spelled in Z gives the identical window", r.json()["balance"] == 50_000.0)

# ---- pending_amount: the money sibling of gb_pending, window-blind ----
with Session(engine) as session:
    acct = session.get(Account, account_id)
    acct.billing_mode = BillingMode.payg
    acct.used_traffic = 5 * 1024 ** 3
    acct.usage_baseline = 0
    session.add(acct)
    settings = get_settings(session)
    settings.default_rate_per_gb = 1000
    session.add(settings)
    session.commit()

r = client.get("/api/ledger/balance", params={"account_id": account_id})
check("pending_amount equals accrued usage x rate", r.json()["pending_amount"] == 5000.0)
check("gb_pending matches the same accrual", r.json()["gb_pending"] == 5.0)
r = client.get("/api/ledger/balance", params={"account_id": account_id, "since": since_after_all})
check("pending_amount is window-blind (same value with and without since)", r.json()["pending_amount"] == 5000.0)

# ---- the «Tabatabaei» case (2026-09-21): a prepay account whose NEW 40 GB package is not invoiced yet ----
# Three old auto-settle charges (two with no GB recorded, one with 5 GB) + a fresh 40 GB package at 5,000 T/GB.
# The widget used to headline only the posted 200,000 while «Owes now» said 400,000, and printed
# «5 GB charged (200,000 T)» — one row's GB next to all three rows' money.
GBYTES = 1024 ** 3
with Session(engine) as session:
    tc = Customer(name="Tabatabaei-like")
    session.add(tc)
    session.commit()
    session.refresh(tc)
    tab = Account(marzban_username="tab_like", customer_id=tc.id, billing_mode=BillingMode.prepay,
                  rate_per_gb=5000, data_limit=40 * GBYTES, billed_data_limit=0, used_traffic=int(2.3 * GBYTES),
                  usage_baseline=0)
    session.add(tab)
    session.commit()
    session.refresh(tab)
    tab_id, tab_cust = tab.id, tc.id
    d0 = now - timedelta(days=40)
    session.add(LedgerEntry(type=LedgerType.charge, amount=150_000, account_id=tab_id, customer_id=tab_cust, date=d0))
    session.add(LedgerEntry(type=LedgerType.charge, amount=25_000, account_id=tab_id, customer_id=tab_cust, date=d0 + timedelta(days=25)))
    session.add(LedgerEntry(type=LedgerType.charge, amount=25_000, account_id=tab_id, customer_id=tab_cust, date=now - timedelta(days=1),
                            gb_amount=5.0, consumed_gb=5.005, consumed_amount=25_025.0))
    session.commit()

wide = (now - timedelta(days=100)).replace(tzinfo=timezone.utc).isoformat()
r = client.get("/api/ledger/balance", params={"account_id": tab_id, "since": wide}).json()
check("Tabatabaei-like: posted balance in the window is 200,000", r["balance"] == 200_000.0)
check("...the unbilled 40 GB package is 200,000 pending", r["pending_amount"] == 200_000.0)
check("...headline net_owed = posted + not invoiced = 400,000 (matches «Owes now»)", r["net_owed"] == 400_000.0)
check("...pending_gb is the BILLABLE 40 GB package, not the live-usage figure (which is <= 2.3 GB)", r["pending_gb"] == 40.0 and r["gb_pending"] <= 2.31)
check("...3 charges in the window, GB recorded on 1", r["charge_count"] == 3 and r["charge_count_with_gb"] == 1)
check("...gb_charged covers only the GB-carrying row (5 GB) and its money is 25,000 of the 200,000",
      r["gb_charged"] == 5.0 and r["charged_amount_gb_known"] == 25_000.0 and r["charged_amount"] == 200_000.0)

recent_since = (now - timedelta(days=2)).replace(tzinfo=timezone.utc).isoformat()
r = client.get("/api/ledger/balance", params={"account_id": tab_id, "since": recent_since}).json()
check("narrower window: only the last charge counts, pending is window-blind, net follows",
      r["charge_count"] == 1 and r["balance"] == 25_000.0 and r["net_owed"] == 225_000.0)

allt = client.get("/api/ledger/balance", params={"account_id": tab_id}).json()
acct_row = [a for a in client.get("/api/customers/%d/accounts" % tab_cust).json() if a["id"] == tab_id][0]
check("all-time net_owed equals the account's own «Owes now» (net_owed on the row)", allt["net_owed"] == acct_row["net_owed"] == 400_000.0)
rc = client.get("/api/ledger/balance", params={"customer_id": tab_cust, "since": wide}).json()
check("customer scope rolls up the same numbers (net 400,000, 40 GB, 3 charges / 1 with GB)",
      rc["net_owed"] == 400_000.0 and rc["pending_gb"] == 40.0 and rc["charge_count"] == 3 and rc["charge_count_with_gb"] == 1)
no_gb = client.get("/api/ledger/balance", params={"account_id": account_id, "since": since_after_all}).json()
check("a window with no charges reports zero counts (not null) and still nets the pending", no_gb["charge_count"] == 0 and no_gb["net_owed"] == no_gb["balance"] + no_gb["pending_amount"])

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All balance-since-date cases passed.")
