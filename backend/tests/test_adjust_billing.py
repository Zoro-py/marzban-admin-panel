"""MONEY-ADJ-1 (2026-09-27 audit): adjust (+GB) + immediate charge double-billed.

The dashboard's Adjust section and the bot's /extend both post an immediate
`/api/ledger` charge for the GB they add, while `/adjust` only grows
`data_limit` — `billed_data_limit` stays put, so the same GB keeps showing as
pending (data_limit - billed_data_limit) and the NEXT prepay settle bills it a
second time. Confirmed on live data: account 39 was charged 300,000 T for a
+60GB adjust on 07-28 and then exactly 300,000 T again by the 08-10 package
settlement, with no reset in between (AccountEvent trail).

The fix contract (D14): the caller that posts the charge first then passes
`bill_added_gb=true` to /adjust, and the endpoint bumps `billed_data_limit` by
exactly that delta (clamped to the new data_limit) in the same transaction.
Without the flag the old behavior is preserved — the added GB shows as pending
and is billed by the next settle (the "bill later"/comp path, still visible).

Plain `python -m tests.test_adjust_billing` from `backend/` — no pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="adjust_test_")) / "test.db"
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
from app.models import Account, AppSettings, BillingMode, Customer  # noqa: E402
from app.services import MoneyBook  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


class FakeMarzban:
    def __init__(self):
        self.panel: dict[str, dict] = {}

    def _user(self, username: str) -> dict:
        return self.panel.setdefault(username, {"username": username, "used_traffic": 0, "status": "active"})

    async def modify_user(self, username: str, payload: dict) -> dict:
        user = self._user(username)
        user.update({k: v for k, v in payload.items() if k in ("data_limit", "expire", "status")})
        return dict(user)


fake = FakeMarzban()
marzban_module.marzban_client.modify_user = fake.modify_user

app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)
GB = 1024 ** 3
RATE = 5000.0

with Session(engine) as session:
    session.add(AppSettings(id=1, default_rate_per_gb=RATE))
    session.commit()


def make_prepay_account(username: str, data_limit_gb: float, billed_gb: float) -> int:
    with Session(engine) as session:
        customer = Customer(name=f"Cust {username}")
        session.add(customer)
        session.commit()
        session.refresh(customer)
        account = Account(
            marzban_username=username,
            customer_id=customer.id,
            billing_mode=BillingMode.prepay,
            data_limit=data_limit_gb * GB,
            billed_data_limit=billed_gb * GB,
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        fake.panel[username] = {"username": username, "used_traffic": 0, "status": "active",
                                "data_limit": account.data_limit}
        return account.id


# ── the Benyamin sequence: charge the added GB now, then adjust with the flag
acc1 = make_prepay_account("adj-bill-now", 30, 30)
r = client.post("/api/ledger", json={
    "type": "charge", "amount": 10 * RATE, "customer_id": 1, "account_id": acc1,
    "note": "+10GB for adj-bill-now",
})
check("caller posts the immediate charge first", r.status_code == 200)

r = client.post(f"/api/accounts/{acc1}/adjust", json={"extend_gb": 10, "bill_added_gb": True})
check("adjust with bill_added_gb succeeds", r.status_code == 200)

with Session(engine) as session:
    acc = session.get(Account, acc1)
    check("billed_data_limit rose with the added GB (30->40)",
          abs((acc.billed_data_limit or 0) - 40 * GB) < 1)
    book = MoneyBook(session)
    check("the added GB no longer shows as pending (was the double-bill source)",
          book.account_pending(acc) == 0.0)

r = client.post(f"/api/accounts/{acc1}/settle", json={})
check("settle succeeds", r.status_code == 200)
check("settle charges ZERO — the same GB is NOT billed a second time (was 10*rate)",
      r.json()["charged_amount"] == 0.0)

# ── flag omitted: the added GB stays pending and IS billed by the next settle
#    (the documented "bill later" path — unchanged, still visible) ──────────
acc2 = make_prepay_account("adj-bill-later", 30, 30)
r = client.post(f"/api/accounts/{acc2}/adjust", json={"extend_gb": 10})
check("adjust without the flag succeeds", r.status_code == 200)
with Session(engine) as session:
    acc = session.get(Account, acc2)
    check("billed_data_limit untouched without the flag", abs((acc.billed_data_limit or 0) - 30 * GB) < 1)
    book = MoneyBook(session)
    check("the added GB shows as pending (visible, billed at settle)",
          abs(book.account_pending(acc) - 10 * RATE) < 0.01)
r = client.post(f"/api/accounts/{acc2}/settle", json={})
check("the later settle bills the added GB exactly once", abs(r.json()["charged_amount"] - 10 * RATE) < 0.01)

# ── clamp: a bump can never push billed_data_limit past the new data_limit ──
acc3 = make_prepay_account("adj-clamp", 30, 30)
r = client.post("/api/ledger", json={
    "type": "charge", "amount": 1 * RATE, "customer_id": 1, "account_id": acc3, "note": "x",
})
r = client.post(f"/api/accounts/{acc3}/adjust", json={"extend_gb": -5, "bill_added_gb": True})
check("negative adjust (shrink) accepted", r.status_code == 200)
r = client.post(f"/api/accounts/{acc3}/adjust", json={"extend_gb": 3, "bill_added_gb": True})
check("re-extend accepted", r.status_code == 200)
with Session(engine) as session:
    acc = session.get(Account, acc3)
    check("billed stays consistent across shrink+re-extend (billed == data == 28GB)",
          abs((acc.billed_data_limit or 0) - 28 * GB) < 1 and abs((acc.data_limit or 0) - 28 * GB) < 1)
    book = MoneyBook(session)
    check("no negative/absurd pending after the clamped bumps", book.account_pending(acc) == 0.0)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All adjust-billing cases passed.")
