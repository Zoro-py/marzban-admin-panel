"""Checks for operator attribution on web-sourced LedgerEntry/AccountEvent
rows — the `created_by` column (raw JWT username, see require_auth).

Covers: a web settle charge + its events carry the operator's username, a
manual /api/ledger entry carries it, sync-sourced rows (created directly the
way sync_job writes them) stay NULL, and rows predating the column read back
as NULL — never an empty string.

Plain `python -m tests.test_created_by` from `backend/`, same harness shape
as tests/test_balance_gb.py (FakeMarzban for the reset path).
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="created_by_test_")) / "test.db"
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

from app import marzban_client as marzban_module  # noqa: E402
from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    AppSettings,
    BillingMode,
    Customer,
    LedgerEntry,
    LedgerSource,
    LedgerType,
    utcnow,
)

init_db()

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


class FakeMarzban:
    pass


# NOTE: assigned straight onto the marzban_client INSTANCE — instance
# attributes don't bind methods, so these plain functions receive the call
# arguments directly (no self).
async def _fake_reset_user(username: str) -> dict:
    return {"username": username, "used_traffic": 0, "status": "active"}


async def _fake_delete_user(username: str) -> None:
    return None


fake = FakeMarzban()
marzban_module.marzban_client.reset_user = _fake_reset_user
marzban_module.marzban_client.delete_user = _fake_delete_user

app.dependency_overrides[require_auth] = lambda: "operator-ali"
client = TestClient(app)

with Session(engine) as session:
    session.add(AppSettings(id=1, default_rate_per_gb=5000))
    cust = Customer(name="Attribution Customer")
    session.add(cust)
    session.commit()
    session.refresh(cust)
    cust_id = cust.id
    acct = Account(
        marzban_username="attr-one", customer_id=cust_id, billing_mode=BillingMode.prepay,
        data_limit=10 * 1024**3, billed_data_limit=0, used_traffic=0,
        usage_baseline=0, usage_baseline_at=utcnow(),
    )
    session.add(acct)
    session.commit()
    session.refresh(acct)
    acct_id = acct.id

    # A row predating the column: written "back then" it simply has no value.
    old = utcnow() - timedelta(days=30)
    session.add(LedgerEntry(
        type=LedgerType.charge, amount=50_000, customer_id=cust_id,
        date=old, source=LedgerSource.web,
    ))
    # A sync-sourced row — sync_job writes these without any operator.
    session.add(LedgerEntry(
        type=LedgerType.charge, amount=25_000, customer_id=cust_id,
        date=utcnow(), source=LedgerSource.sync,
    ))
    session.commit()

# ---- web settle: charge + credit + events carry the operator username ----
r = client.post(f"/api/accounts/{acct_id}/settle", json={"mark_paid": True})
check("settle succeeds", r.status_code == 200)

with Session(engine) as session:
    charge = (
        session.query(LedgerEntry)
        .filter(LedgerEntry.account_id == acct_id, LedgerEntry.type == LedgerType.charge)
        .order_by(LedgerEntry.id.desc())
        .first()
    )
    credit = (
        session.query(LedgerEntry)
        .filter(LedgerEntry.account_id == acct_id, LedgerEntry.type == LedgerType.credit)
        .first()
    )
    check("web settle charge attributed to 'operator-ali'", charge.created_by == "operator-ali")
    check("web mark-paid credit attributed too", credit.created_by == "operator-ali")
    check("charge's source stays 'web'", charge.source == LedgerSource.web)

# ---- manual ledger entry: attributed; sync rows and legacy rows are not ----
r = client.post("/api/ledger", json={"type": "credit", "amount": 1_000, "customer_id": cust_id})
check("manual entry accepted", r.status_code == 200)
check("manual entry attributed to 'operator-ali'", r.json()["created_by"] == "operator-ali")

with Session(engine) as session:
    rows = session.query(LedgerEntry).order_by(LedgerEntry.id).all()
    # Row 1 = the backdated web charge written before the column existed.
    legacy = rows[0]
    sync_row = next(r for r in rows if r.source == LedgerSource.sync)
    check("legacy pre-column web row reads NULL (not empty string)", legacy.created_by is None)
    check("sync-sourced row stays NULL", sync_row.created_by is None)

# ---- sync-style AccountEvent (written the way sync_job writes them) vs web ----
from app.models import AccountEvent  # noqa: E402

with Session(engine) as session:
    session.add(AccountEvent(
        account_id=acct_id, action="external_expire_extend",
        detail="+1 day (in Marzban directly)", source=LedgerSource.sync,
    ))
    session.commit()
    sync_evt = (
        session.query(AccountEvent)
        .filter(AccountEvent.action == "external_expire_extend")
        .first()
    )
    web_evt = (
        session.query(AccountEvent)
        .filter(AccountEvent.action == "settle_reset")
        .first()
    )
    check("sync-sourced event stays NULL", sync_evt.created_by is None)
    check("web settle event attributed", web_evt.created_by == "operator-ali")

# ---- deletion close-out passes the operator through the helper ----
# close_out only fires for PAYG accounts with unbilled usage (prepay has no
# live meter reading to lose), so this one gets an open payg meter.
with Session(engine) as session:
    payg = Account(
        marzban_username="attr-payg", customer_id=cust_id, billing_mode=BillingMode.payg,
        data_limit=50 * 1024**3, used_traffic=6 * 1024**3,
        usage_baseline=0, usage_baseline_at=utcnow(),
    )
    session.add(payg)
    session.commit()
    session.refresh(payg)
    payg_id = payg.id

r = client.post(f"/api/accounts/{payg_id}/delete")
check("payg delete succeeds", r.status_code == 200)
if r.status_code != 200:
    print("    delete response:", r.text[:300])
    import app.marzban_client as mc
    print("    delete_user is:", mc.marzban_client.__dict__.get("delete_user", "REAL METHOD"))
with Session(engine) as session:
    closeout = (
        session.query(LedgerEntry)
        .filter(LedgerEntry.note == "Final payg usage before delete")
        .first()
    )
    check("close-out charge posted", closeout is not None)
    check("close-out charge attributed to the operator", closeout.created_by == "operator-ali")
    check("close-out source stays 'web'", closeout.source == LedgerSource.web)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All created_by attribution cases passed.")
