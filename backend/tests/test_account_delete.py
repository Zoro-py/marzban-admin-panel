"""Checks for the operator-facing POST /api/accounts/{account_id}/delete —
the first time account deletion existed anywhere for the OPERATOR (it
previously only existed for a Delegate acting on their own scoped
accounts; see delegate_service.py). Shares the same
close_out_payg_usage_before_delete / cancel_pending_queued_plan safety
nets from app/services.py, checked here from the operator's side.

Plain `python -m tests.test_account_delete` from `backend/`, same harness
shape as tests/test_delegate_smoke.py.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="account_delete_test_")) / "test.db"
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

from app import marzban_client as marzban_module  # noqa: E402
from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, AppSettings, BillingMode, Customer, LedgerEntry, QueuedPlan, QueuedPlanStatus  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


class FakeMarzban:
    def __init__(self):
        self.deleted: list[str] = []
        self.fail_delete: bool = False

    async def delete_user(self, username: str) -> None:
        if self.fail_delete:
            raise ValueError("simulated Marzban rejection")
        self.deleted.append(username)


fake = FakeMarzban()
marzban_module.marzban_client.delete_user = fake.delete_user

app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)

with Session(engine) as session:
    session.add(AppSettings(id=1, default_rate_per_gb=1000))
    customer = Customer(name="Delete Test Customer")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    customer_id = customer.id

    prepay_account = Account(marzban_username="prepay-del", customer_id=customer_id,
                             billing_mode=BillingMode.prepay, data_limit=10 * 1024**3, billed_data_limit=10 * 1024**3)
    payg_account = Account(marzban_username="payg-del", customer_id=customer_id,
                           billing_mode=BillingMode.payg, used_traffic=3 * 1024**3, usage_baseline=0)
    session.add(prepay_account)
    session.add(payg_account)
    session.commit()
    session.refresh(prepay_account)
    session.refresh(payg_account)
    prepay_id = prepay_account.id
    payg_id = payg_account.id

    session.add(QueuedPlan(account_id=prepay_id, data_limit_gb=10, duration_days=30))
    session.commit()

# ---- delete a prepay account with a pending queued plan ----
r = client.post(f"/api/accounts/{prepay_id}/delete")
check("prepay delete succeeds (200)", r.status_code == 200)
check("Marzban delete_user was actually called", "prepay-del" in fake.deleted)
with Session(engine) as session:
    acc = session.get(Account, prepay_id)
    check("account soft-deleted (deleted_at set)", acc is not None and acc.deleted_at is not None)
    plans = session.exec(select(QueuedPlan).where(QueuedPlan.account_id == prepay_id)).all()
    check("pending queued plan was cancelled", plans[0].status == QueuedPlanStatus.cancelled)
    entries = session.exec(select(LedgerEntry).where(LedgerEntry.account_id == prepay_id)).all()
    check("prepay delete posts no charge (nothing outstanding)", len(entries) == 0)

# ---- deleted accounts excluded from the dashboard's own list ----
r = client.get("/api/accounts")
check("deleted account excluded from /api/accounts", prepay_id not in [a["id"] for a in r.json()])

# ---- deleting again is refused, not a silent no-op ----
r = client.post(f"/api/accounts/{prepay_id}/delete")
check("deleting an already-deleted account is refused (400)", r.status_code == 400)

# ---- unknown account ----
r = client.post("/api/accounts/999999/delete")
check("unknown account_id returns 404", r.status_code == 404)

# ---- payg account with unbilled usage: final charge posted + baseline rolled ----
r = client.post(f"/api/accounts/{payg_id}/delete")
check("payg delete succeeds", r.status_code == 200)
with Session(engine) as session:
    entries = session.exec(select(LedgerEntry).where(LedgerEntry.account_id == payg_id)).all()
    check("a final charge for the 3GB unbilled payg usage was posted", len(entries) == 1 and entries[0].amount == 3000.0)
    acc = session.get(Account, payg_id)
    check("usage_baseline rolled forward so it doesn't also show as pending", acc.usage_baseline == acc.used_traffic)

# ---- a failed Marzban call posts no charge and does not soft-delete ----
with Session(engine) as session:
    fail_account = Account(marzban_username="fail-del", customer_id=customer_id,
                           billing_mode=BillingMode.payg, used_traffic=2 * 1024**3, usage_baseline=0)
    session.add(fail_account)
    session.commit()
    session.refresh(fail_account)
    fail_id = fail_account.id
fake.fail_delete = True
r = client.post(f"/api/accounts/{fail_id}/delete")
check("a Marzban rejection surfaces as 400, not a silent success", r.status_code == 400)
with Session(engine) as session:
    acc = session.get(Account, fail_id)
    check("account NOT soft-deleted when Marzban rejected the delete", acc is not None and acc.deleted_at is None)
    entries = session.exec(select(LedgerEntry).where(LedgerEntry.account_id == fail_id)).all()
    check("no charge posted when the Marzban call failed (computed-before, written-after ordering held)", len(entries) == 0)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All operator account-delete cases passed.")
