"""Smoke test: does the app boot with the new Delegate table/router, does
init_db() create the table + Account.deleted_at column, and does the basic
create -> list -> renew -> delete flow work end-to-end through the real
FastAPI app (TestClient + FakeMarzban, same harness as tests/test_shop.py)?

Plain `python -m tests.test_delegate_smoke` from `backend/`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="delegate_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""
os.environ["SHOP_BOT_API_KEY"] = ""
os.environ["DELEGATE_BOT_API_KEY"] = "test-delegate-key"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import marzban_client as marzban_module  # noqa: E402
from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, AppSettings, Customer, Delegate, LedgerEntry  # noqa: E402

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

    async def create_user(self, payload: dict) -> dict:
        user = dict(payload)
        user["used_traffic"] = 0
        user["lifetime_used_traffic"] = 0
        self.panel[payload["username"]] = user
        return user

    async def modify_user(self, username: str, payload: dict) -> dict:
        user = self.panel[username]
        user.update({k: v for k, v in payload.items() if k in ("data_limit", "expire", "status")})
        return dict(user)

    async def delete_user(self, username: str) -> None:
        self.panel.pop(username, None)

    async def list_all_users(self) -> list[dict]:
        return [dict(u, username=name) for name, u in self.panel.items()]


fake = FakeMarzban()
marzban_module.marzban_client.create_user = fake.create_user
marzban_module.marzban_client.modify_user = fake.modify_user
marzban_module.marzban_client.delete_user = fake.delete_user
marzban_module.marzban_client.list_all_users = fake.list_all_users

app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)
BOT_HEADERS = {"X-Delegate-Bot-Key": "test-delegate-key"}

with engine.begin() as conn:
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(account)"))}
    check("Account.deleted_at column exists after init_db()", "deleted_at" in cols)
    tables = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    check("delegate table exists after init_db()", "delegate" in tables)

with Session(engine) as session:
    # A rate must be configured somewhere in the fallback chain (account ->
    # group -> dashboard default) or effective_rate is 0 and no charge posts
    # at all — same "if amount > 0" convention as settle_account.
    session.add(AppSettings(id=1, default_rate_per_gb=1000))
    customer = Customer(name="Test Reseller Customer")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    customer_id = customer.id

# Operator creates the delegate (create_or_update_delegate, require_auth).
r = client.post("/api/delegate", json={"customer_id": customer_id, "telegram_id": 9001, "credit_limit": 50000, "daily_create_cap": 2})
check("delegate created (200)", r.status_code == 200)
delegate_body = r.json()
check("delegate scope_name reflects the customer", delegate_body["scope_name"] == "Test Reseller Customer")

# Wrong bot key rejected (auth boundary).
r = client.post("/api/delegate/bot/session", headers={"X-Delegate-Bot-Key": "wrong"}, json={"telegram_id": 9001})
check("wrong delegate bot key rejected", r.status_code == 401)

# Unknown telegram_id rejected.
r = client.post("/api/delegate/bot/session", headers=BOT_HEADERS, json={"telegram_id": 424242})
check("unknown telegram_id rejected (403)", r.status_code == 403)

# Real session.
r = client.post("/api/delegate/bot/session", headers=BOT_HEADERS, json={"telegram_id": 9001})
check("session ok", r.status_code == 200 and r.json()["scope_name"] == "Test Reseller Customer")

# Create an account.
r = client.post("/api/delegate/bot/accounts", headers=BOT_HEADERS, json={"telegram_id": 9001, "data_limit_gb": 10})
check("account created", r.status_code == 200)
account_id = r.json()["id"]
check("username auto-generated with the 'd' prefix", r.json()["marzban_username"] == "d1")

with Session(engine) as session:
    entries = session.exec(select(LedgerEntry).where(LedgerEntry.account_id == account_id)).all()
    check("a charge was auto-posted on create", len(entries) == 1 and entries[0].amount > 0)

# List accounts.
r = client.get("/api/delegate/bot/accounts", headers=BOT_HEADERS, params={"telegram_id": 9001})
check("list shows exactly the one account", r.status_code == 200 and len(r.json()) == 1)

# Renew.
r = client.post(f"/api/delegate/bot/accounts/{account_id}/renew", headers=BOT_HEADERS,
                json={"telegram_id": 9001, "extend_gb": 5})
check("renew ok", r.status_code == 200)
with Session(engine) as session:
    entries = session.exec(select(LedgerEntry).where(LedgerEntry.account_id == account_id)).all()
    check("a second charge was posted on renew", len(entries) == 2)

# Daily cap: cap was set to 2, one account already created -> one more allowed, then blocked.
r = client.post("/api/delegate/bot/accounts", headers=BOT_HEADERS, json={"telegram_id": 9001, "data_limit_gb": 5})
check("second create within cap succeeds", r.status_code == 200)
r = client.post("/api/delegate/bot/accounts", headers=BOT_HEADERS, json={"telegram_id": 9001, "data_limit_gb": 5})
check("third create hits the daily cap (400)", r.status_code == 400 and "سقف" in r.json()["detail"])

# Can't touch someone else's account: create a second, unrelated delegate/customer.
with Session(engine) as session:
    other_customer = Customer(name="Other Customer")
    session.add(other_customer)
    session.commit()
    session.refresh(other_customer)
    other_customer_id = other_customer.id
client.post("/api/delegate", json={"customer_id": other_customer_id, "telegram_id": 9002, "daily_create_cap": 5})
r = client.post(f"/api/delegate/bot/accounts/{account_id}/renew", headers=BOT_HEADERS,
                json={"telegram_id": 9002, "extend_gb": 1})
check("a different delegate cannot renew this account (400, not found in their scope)", r.status_code == 400)

# Delete.
r = client.post(f"/api/delegate/bot/accounts/{account_id}/delete", headers=BOT_HEADERS, json={"telegram_id": 9001})
check("delete ok", r.status_code == 200)
r = client.get("/api/delegate/bot/accounts", headers=BOT_HEADERS, params={"telegram_id": 9001})
check("deleted account no longer listed", account_id not in [a["id"] for a in r.json()])
with Session(engine) as session:
    acc = session.get(Account, account_id)
    check("account row still exists (soft delete), deleted_at set", acc is not None and acc.deleted_at is not None)
    entries = session.exec(select(LedgerEntry).where(LedgerEntry.account_id == account_id)).all()
    check("delete posted no new charge", len(entries) == 2)

# Deactivated delegate is locked out.
r = client.post(f"/api/delegate/{delegate_body['id']}/deactivate")
check("deactivate ok", r.status_code == 200)
r = client.post("/api/delegate/bot/accounts", headers=BOT_HEADERS, json={"telegram_id": 9001, "data_limit_gb": 1})
check("deactivated delegate is refused (403)", r.status_code == 403)

# Dashboard's own account list excludes soft-deleted accounts.
r = client.get("/api/accounts")
check("dashboard account list excludes the deleted one", account_id not in [a["id"] for a in r.json()])

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All delegate smoke-test cases passed.")
