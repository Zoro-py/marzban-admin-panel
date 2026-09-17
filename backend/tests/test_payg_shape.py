"""Checks for update_billing's payg shape rule: switching a STANDALONE
account to payg applies the payg standard — no expiry and a 300GB soft cap —
in Marzban first, then mirrored locally, with the shape noted on the
billing_change event. Grouped accounts and payg→payg no-op writes must not
trigger it (a grouped account's effective mode is the group's, and a
re-POST of an already-payg account is not a switch).

Plain `python -m tests.test_payg_shape` from `backend/`, same harness shape
as tests/test_created_by.py.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="payg_shape_test_")) / "test.db"
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
    AccountEvent,
    AppSettings,
    BillingMode,
    Customer,
    Group,
    utcnow,
)

init_db()

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


# Assigned straight onto the marzban_client INSTANCE — instance attributes
# don't bind methods, so this plain function receives the call arguments
# directly (no self).
marzban_calls: list[tuple[str, dict]] = []


async def _fake_modify_user(username: str, payload: dict) -> dict:
    marzban_calls.append((username, payload))
    # Marzban's own convention: expire=0 means "never expires".
    return {"username": username, "expire": 0, "data_limit": payload.get("data_limit"), "status": "active"}


marzban_module.marzban_client.modify_user = _fake_modify_user

app.dependency_overrides[require_auth] = lambda: "shape-operator"
client = TestClient(app)

with Session(engine) as session:
    session.add(AppSettings(id=1, default_rate_per_gb=5000))
    cust = Customer(name="Payg Shape Customer")
    session.add(cust)
    session.commit()
    session.refresh(cust)
    cust_id = cust.id
    standalone = Account(
        marzban_username="shape-standalone", customer_id=cust_id, billing_mode=BillingMode.prepay,
        data_limit=5 * 1024**3, expire=int(utcnow().timestamp()) + 86400,
    )
    session.add(standalone)
    grouped = Account(
        marzban_username="shape-grouped", group_id=None, billing_mode=BillingMode.prepay,
        data_limit=5 * 1024**3, expire=int(utcnow().timestamp()) + 86400,
    )
    session.add(grouped)
    grp = Group(name="Shape Group", representative_customer_id=cust_id)
    session.add(grp)
    session.commit()
    session.refresh(standalone)
    session.refresh(grouped)
    session.refresh(grp)
    standalone_id, grouped_id, grp_id = standalone.id, grouped.id, grp.id
    grouped.group_id = grp_id
    session.add(grouped)
    session.commit()

# 1) standalone prepay -> payg: shape applies in Marzban and locally.
r = client.patch(f"/api/accounts/{standalone_id}/billing", json={"billing_mode": "payg"})
check("switch to payg succeeds", r.status_code == 200)
check("Marzban was called with expire=0 and the 300GB cap", marzban_calls and marzban_calls[-1][1] == {"expire": 0, "data_limit": int(300 * 1024**3)})
with Session(engine) as session:
    a = session.get(Account, standalone_id)
    check("local mirror: expire cleared (None = never)", a.expire is None)
    check("local mirror: data_limit is 300GB", a.data_limit == int(300 * 1024**3))
    check("local mirror: billing_mode is payg", a.billing_mode == BillingMode.payg)
with Session(engine) as session:
    events = session.exec(
        __import__("sqlmodel").select(AccountEvent).where(AccountEvent.account_id == standalone_id)
    ).all()
check("billing_change event notes the applied shape", any("payg shape applied" in (e.detail or "") for e in events))

# 2) re-POSTing payg on an already-payg account is NOT a switch.
calls_before = len(marzban_calls)
client.patch(f"/api/accounts/{standalone_id}/billing", json={"billing_mode": "payg"})
check("payg->payg re-POST triggers no Marzban call", len(marzban_calls) == calls_before)

# 3) grouped account: switching its own raw field does not shape it — the
#    group's mode governs it, and its limits belong to the group.
client.patch(f"/api/accounts/{grouped_id}/billing", json={"billing_mode": "payg"})
check("grouped account: no Marzban call on mode switch", len(marzban_calls) == calls_before)
with Session(engine) as session:
    g = session.get(Account, grouped_id)
    check("grouped account: limits untouched", g.expire is not None and g.data_limit == int(5 * 1024**3))

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All payg-shape cases passed.")
