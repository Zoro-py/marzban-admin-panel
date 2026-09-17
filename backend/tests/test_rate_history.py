"""Checks for the structured rate-change audit trail (models.RateChange):
every change to an account's rate, a group's rate, or the dashboard-wide
default gets a row with the OLD and NEW value (NULL = "unset", which means
"inherited" in the effective_rate chain — never rewritten to 0), the
operator's username, and the right scope.

Covers: set / change / clear at all three scopes, no row when nothing
actually changed, and scope-scoped reads from /api/settings/rate-changes
(an account's history must not mix in the group's or the default's rows).

Plain `python -m tests.test_rate_history` from `backend/`, same harness
shape as tests/test_created_by.py.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="rate_history_test_")) / "test.db"
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
from app.models import Account, Customer, Group, RateChange  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


app.dependency_overrides[require_auth] = lambda: "rate-operator"
client = TestClient(app)

with Session(engine) as session:
    cust = Customer(name="Rate History Customer")
    session.add(cust)
    session.commit()
    session.refresh(cust)
    cust_id = cust.id
    acct = Account(marzban_username="rate-acct", customer_id=cust_id)
    session.add(acct)
    session.commit()
    session.refresh(acct)
    acct_id = acct.id
    grp = Group(name="Rate Group", representative_customer_id=cust_id)
    session.add(grp)
    session.commit()
    session.refresh(grp)
    grp_id = grp.id


def rows_for(**params) -> list[RateChange]:
    with Session(engine) as session:
        stmt = session.query(RateChange).order_by(RateChange.id)
        if "account_id" in params:
            stmt = stmt.filter(RateChange.account_id == params["account_id"])
        if "group_id" in params:
            stmt = stmt.filter(RateChange.group_id == params["group_id"])
        if params.get("scope"):
            stmt = stmt.filter(RateChange.scope == params["scope"])
        return stmt.all()


# ═══════════════════════════════════ account scope ═══════════════════════════
# Set (was unset), change, clear — three rows, old/new honest about NULL.
client.patch(f"/api/accounts/{acct_id}/billing", json={"rate_per_gb": 5000})
client.patch(f"/api/accounts/{acct_id}/billing", json={"rate_per_gb": 6000})
client.patch(f"/api/accounts/{acct_id}/billing", json={"clear_rate": True})

rows = rows_for(account_id=acct_id)
check("account rate: three changes recorded", len(rows) == 3)
check("set: old None (was unset), new 5000", rows[0].old_rate is None and rows[0].new_rate == 5000.0)
check("change: old 5000, new 6000", rows[1].old_rate == 5000.0 and rows[1].new_rate == 6000.0)
check("clear: old 6000, new None (inherited, NOT zero)", rows[2].old_rate == 6000.0 and rows[2].new_rate is None)
check("all three attributed to the operator", all(r.created_by == "rate-operator" for r in rows))
check("scope recorded as 'account'", all(r.scope == "account" for r in rows))

# No-op patch (same value re-sent) must NOT add a row — the audit trail
# records CHANGES, not every write that happened to touch the field.
n_before = len(rows_for(account_id=acct_id))
client.patch(f"/api/accounts/{acct_id}/billing", json={"billing_mode": "payg"})  # rate untouched
client.patch(f"/api/accounts/{acct_id}/billing", json={"clear_rate": True})  # already cleared
check("no-op rate writes record nothing", len(rows_for(account_id=acct_id)) == n_before)

# ══════════════════════════════════════ group scope ══════════════════════════
client.patch(f"/api/groups/{grp_id}", json={"rate_per_gb": 4000})
client.patch(f"/api/groups/{grp_id}", json={"rate_per_gb": 4500, "name": "Rate Group Renamed"})

rows = rows_for(group_id=grp_id)
check("group rate: two changes recorded", len(rows) == 2)
check("group set: old None, new 4000", rows[0].old_rate is None and rows[0].new_rate == 4000.0)
check("group change: old 4000, new 4500", rows[1].old_rate == 4000.0 and rows[1].new_rate == 4500.0)
check("group rows attributed + scoped", all(r.created_by == "rate-operator" and r.scope == "group" for r in rows))

# ══════════════════════════════════════ default scope ════════════════════════
client.patch("/api/settings", json={"default_rate_per_gb": 5000})
client.patch("/api/settings", json={"default_rate_per_gb": 3000})

rows = rows_for(scope="default")
check("default rate: two changes recorded", len(rows) == 2)
check("default set: old None, new 5000", rows[0].old_rate is None and rows[0].new_rate == 5000.0)
check("default change: old 5000, new 3000", rows[1].old_rate == 5000.0 and rows[1].new_rate == 3000.0)
check("default rows attributed + scoped", all(r.created_by == "rate-operator" and r.scope == "default" for r in rows))

# ════════════════════════════════════ scoped reads ═══════════════════════════
# The endpoint must NOT mix scopes: an account's history is its own field
# only, otherwise "did THIS rate change?" becomes ambiguous again.
ra = client.get("/api/settings/rate-changes", params={"account_id": acct_id}).json()
check("account read: only account rows", len(ra) == 3 and all(r["scope"] == "account" for r in ra))
rg = client.get("/api/settings/rate-changes", params={"group_id": grp_id}).json()
check("group read: only group rows", len(rg) == 2 and all(r["scope"] == "group" for r in rg))
rd = client.get("/api/settings/rate-changes").json()
check("global read: only default rows", len(rd) == 2 and all(r["scope"] == "default" for r in rd))
check("read rows carry created_by", all(r.get("created_by") == "rate-operator" for r in ra + rg + rd))

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All rate-history cases passed.")
