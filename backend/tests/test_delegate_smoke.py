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
from app.models import Account, AppSettings, BillingMode, Customer, Delegate, Group, LedgerEntry, QueuedPlan, QueuedPlanStatus  # noqa: E402

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

# ── credit_limit is actually enforced by real posted debt, not just the
# daily count cap (delegate 9001's daily cap of 2 was already hit above, so
# use a fresh delegate/customer with a high daily cap and a tight credit
# limit to isolate this check). ─────────────────────────────────────────
with Session(engine) as session:
    capped_customer = Customer(name="Capped Customer")
    session.add(capped_customer)
    session.commit()
    session.refresh(capped_customer)
    capped_customer_id = capped_customer.id
client.post("/api/delegate", json={"customer_id": capped_customer_id, "telegram_id": 9003,
                                   "credit_limit": 5000, "daily_create_cap": 50})
r = client.post("/api/delegate/bot/accounts", headers=BOT_HEADERS, json={"telegram_id": 9003, "data_limit_gb": 10})
check("first create under the credit limit succeeds (10GB*1000=10000 > 5000 cap not yet checked before this one)",
      r.status_code == 200)
r2 = client.post("/api/delegate/bot/accounts", headers=BOT_HEADERS, json={"telegram_id": 9003, "data_limit_gb": 1})
check("a second create is refused once posted debt >= credit_limit", r2.status_code == 400 and "بدهی" in r2.json()["detail"])

# ── partial upsert: re-running /delegate_add-style (telegram_id + only
# customer_id) must NOT wipe the credit_limit just set above. ───────────
r = client.post("/api/delegate", json={"customer_id": capped_customer_id, "telegram_id": 9003})
check("partial re-post (no credit_limit key) preserves the existing credit_limit",
      r.status_code == 200 and r.json()["credit_limit"] == 5000)
# And /delegate_cap-style (telegram_id + only credit_limit) must not touch
# customer_id/daily_create_cap.
r = client.post("/api/delegate", json={"telegram_id": 9003, "credit_limit": None})
check("partial re-post (credit_limit=None explicitly) clears just that field",
      r.status_code == 200 and r.json()["credit_limit"] is None and r.json()["daily_create_cap"] == 50)

# ── renewing an account that's already expired extends from NOW, not from
# the stale past expire (the old `if account.expire else now` bug). ─────
with Session(engine) as session:
    expired_customer = Customer(name="Expired Renew Customer")
    session.add(expired_customer)
    session.commit()
    session.refresh(expired_customer)
    expired_customer_id = expired_customer.id
client.post("/api/delegate", json={"customer_id": expired_customer_id, "telegram_id": 9004, "daily_create_cap": 10})
r = client.post("/api/delegate/bot/accounts", headers=BOT_HEADERS, json={"telegram_id": 9004, "data_limit_gb": 5})
expired_account_id = r.json()["id"]
import time as _time
with Session(engine) as session:
    acc = session.get(Account, expired_account_id)
    acc.expire = int(_time.time()) - 60 * 86400  # expired 60 days ago
    session.add(acc)
    session.commit()
r = client.post(f"/api/delegate/bot/accounts/{expired_account_id}/renew", headers=BOT_HEADERS,
                json={"telegram_id": 9004, "extend_gb": 5, "extend_days": 10})
check("renewing an expired account succeeds", r.status_code == 200)
check("the renewed expiry is in the future (extended from now, not the stale past expire)",
      r.json()["expire"] > int(_time.time()))

# ── deleting an account with a PENDING QueuedPlan cancels it, so it doesn't
# sit forever as a phantom future charge on /api/reports/upcoming-renewals
# for an account that no longer exists to ever activate it. ─────────────
with Session(engine) as session:
    qp_customer = Customer(name="Queued Plan Customer")
    session.add(qp_customer)
    session.commit()
    session.refresh(qp_customer)
    qp_customer_id = qp_customer.id
client.post("/api/delegate", json={"customer_id": qp_customer_id, "telegram_id": 9007, "daily_create_cap": 10})
r = client.post("/api/delegate/bot/accounts", headers=BOT_HEADERS, json={"telegram_id": 9007, "data_limit_gb": 5})
qp_account_id = r.json()["id"]
with Session(engine) as session:
    session.add(QueuedPlan(account_id=qp_account_id, data_limit_gb=10, duration_days=30))
    session.commit()
r = client.get("/api/reports/upcoming-renewals")
check("the pending plan shows up in upcoming-renewals before delete",
      qp_account_id in [row.get("account_id") for row in r.json()])
r = client.post(f"/api/delegate/bot/accounts/{qp_account_id}/delete", headers=BOT_HEADERS, json={"telegram_id": 9007})
check("delete with a pending queued plan succeeds", r.status_code == 200)
with Session(engine) as session:
    plans = session.exec(select(QueuedPlan).where(QueuedPlan.account_id == qp_account_id)).all()
    check("the pending plan was cancelled on delete, not left dangling",
          len(plans) == 1 and plans[0].status == QueuedPlanStatus.cancelled)
r = client.get("/api/reports/upcoming-renewals")
check("the deleted account's plan no longer shows as an upcoming renewal",
      qp_account_id not in [row.get("account_id") for row in r.json()])

# ── group delegates: scope isolation + ledger entries carry group_id so
# reports.py can show WHICH group a charge belongs to. ──────────────────
with Session(engine) as session:
    group_rep = Customer(name="Group Rep")
    session.add(group_rep)
    session.commit()
    session.refresh(group_rep)
    group = Group(name="Test Group", representative_customer_id=group_rep.id, billing_mode=BillingMode.prepay)
    session.add(group)
    session.commit()
    session.refresh(group)
    group_id = group.id
client.post("/api/delegate", json={"group_id": group_id, "telegram_id": 9005, "daily_create_cap": 10})
r = client.post("/api/delegate/bot/accounts", headers=BOT_HEADERS, json={"telegram_id": 9005, "data_limit_gb": 10})
check("group delegate can create", r.status_code == 200)
group_account_id = r.json()["id"]
with Session(engine) as session:
    acc = session.get(Account, group_account_id)
    check("the created account is attached to the GROUP, not a customer", acc.group_id == group_id and acc.customer_id is None)
    entry = session.exec(select(LedgerEntry).where(LedgerEntry.account_id == group_account_id)).first()
    check("the ledger entry for a group delegate's charge carries group_id", entry.group_id == group_id)
# A customer delegate must never reach a group account, and vice versa.
r = client.post(f"/api/delegate/bot/accounts/{group_account_id}/renew", headers=BOT_HEADERS,
                json={"telegram_id": 9003, "extend_gb": 1})
check("a customer delegate cannot renew a group account", r.status_code == 400)

# ── deleting a PAYG account bills its outstanding usage first (the "final
# usage before delete" safety net) — everything this service creates is
# prepay, so build a payg account directly to exercise this path. ───────
with Session(engine) as session:
    payg_customer = Customer(name="Payg Delete Customer")
    session.add(payg_customer)
    session.commit()
    session.refresh(payg_customer)
    payg_account = Account(marzban_username="payg-acct", customer_id=payg_customer.id,
                           billing_mode=BillingMode.payg, used_traffic=5 * 1024**3, usage_baseline=0)
    session.add(payg_account)
    session.commit()
    session.refresh(payg_account)
    payg_account_id = payg_account.id
    payg_customer_id = payg_customer.id
fake.panel["payg-acct"] = {"username": "payg-acct"}  # so delete_user has something to remove
client.post("/api/delegate", json={"customer_id": payg_customer_id, "telegram_id": 9006, "daily_create_cap": 10})
r = client.post(f"/api/delegate/bot/accounts/{payg_account_id}/delete", headers=BOT_HEADERS, json={"telegram_id": 9006})
check("deleting a payg account with unbilled usage succeeds", r.status_code == 200)
with Session(engine) as session:
    entries = session.exec(select(LedgerEntry).where(LedgerEntry.account_id == payg_account_id)).all()
    check("a final charge for the 5GB unbilled payg usage was posted before delete",
          len(entries) == 1 and entries[0].amount == 5000.0)  # 5GB * 1000 rate
    acc = session.get(Account, payg_account_id)
    check("usage_baseline rolled forward so the just-billed usage doesn't ALSO show as pending forever",
          acc.usage_baseline == acc.used_traffic)

# The dashboard's own "pending" figure for this now-deleted account must be
# 0 — not the 5GB it was already charged for at delete time. MoneyBook
# still counts deleted accounts (their real debt is still real debt), so
# this is specifically checking the baseline-roll fix, not deleted_at
# filtering.
from app.services import MoneyBook  # noqa: E402
with Session(engine) as session:
    book = MoneyBook(session)
    acc = session.get(Account, payg_account_id)
    check("pending for the deleted payg account is 0 after the baseline roll",
          book.account_pending(acc) == 0.0)

# ── a deleted GROUP member is excluded from settle_group, so it doesn't
# fail that group's settlement forever. ──────────────────────────────────
r = client.post(f"/api/delegate/bot/accounts/{group_account_id}/delete", headers=BOT_HEADERS, json={"telegram_id": 9005})
check("group member delete ok", r.status_code == 200)
r = client.post(f"/api/groups/{group_id}/settle", json={"mark_paid": False})
check("settling the group after its only member was deleted doesn't error",
      r.status_code == 200 and "payg-acct" not in str(r.json().get("failed_resets", [])))

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All delegate smoke-test cases passed.")
