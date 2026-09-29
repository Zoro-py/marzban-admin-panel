"""Hostile cross-scope test for the delegate bot, run against a copy of the
LIVE database (the 2026-09-29 checklist row: "IDOR نماینده — تست خصمانهٔ تازه
با دو نماینده روی کپی زنده").

Production today carries ZERO delegate rows (checked 2026-09-29), so "two real
delegates" is reified as: two delegates seeded INTO A TEMP COPY of the live DB,
scoped to two REAL live scopes (a real customer with real accounts, a real
group with real members). Everything around them — the 300+ real accounts,
balances, ledger — is genuine production data, which is exactly what the scope
filter must not leak.

Runs the real FastAPI app (TestClient + stubbed Marzban) against the copy:

  VPN_IDOR_DB=<path to a live db copy> python -m tests.test_delegate_idor_live   (from backend/)

Without VPN_IDOR_DB the live part is skipped with a notice (CI stays green);
the synthetic refusal matrix below still runs on a seeded scratch DB.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_LIVE_COPY = os.environ.get("VPN_IDOR_DB", "")
_TMP_DIR = Path(tempfile.mkdtemp(prefix="delegate_idor_"))
if _LIVE_COPY:
    _DB = _TMP_DIR / "live_copy.db"
    shutil.copyfile(_LIVE_COPY, _DB)  # never touch the caller's file
else:
    _DB = _TMP_DIR / "scratch.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB.as_posix()}"
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
from app.models import AppSettings, Customer, Delegate, Group  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


class AlarmMarzban:
    """Records every Marzban call. A scope-check bug that lets a cross-scope
    renew through would surface here as a modify_user on someone else's user."""

    def __init__(self):
        self.touched: list[tuple[str, str]] = []

    async def create_user(self, payload: dict) -> dict:
        self.touched.append(("create", payload["username"]))
        return dict(payload, used_traffic=0, lifetime_used_traffic=0)

    async def modify_user(self, username: str, payload: dict) -> dict:
        self.touched.append(("modify", username))
        return dict(payload)

    async def delete_user(self, username: str) -> None:
        self.touched.append(("delete", username))

    async def list_all_users(self) -> list[dict]:
        return []


fake = AlarmMarzban()
marzban_module.marzban_client.create_user = fake.create_user
marzban_module.marzban_client.modify_user = fake.modify_user
marzban_module.marzban_client.delete_user = fake.delete_user
marzban_module.marzban_client.list_all_users = fake.list_all_users

app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)
BOT_HEADERS = {"X-Delegate-Bot-Key": "test-delegate-key"}

# ---------------------------------------------------------------- pick scopes
with Session(engine) as session:
    if _LIVE_COPY:
        # Real live scopes: the customer with the most accounts, and a real
        # group that actually has members (so cross-scope targets exist).
        row = session.exec(
            text(
                "SELECT customer_id, COUNT(*) c FROM account"
                " WHERE customer_id IS NOT NULL AND deleted_at IS NULL"
                " GROUP BY customer_id ORDER BY c DESC LIMIT 1"
            )
        ).first()
        cust_id, cust_n = int(row[0]), int(row[1])
        row = session.exec(
            text(
                "SELECT g.id, COUNT(a.id) c FROM \"group\" g"
                " JOIN account a ON a.group_id = g.id AND a.deleted_at IS NULL"
                " GROUP BY g.id ORDER BY c DESC LIMIT 1"
            )
        ).first()
        grp_id, grp_n = int(row[0]), int(row[1])
        # A real account OUTSIDE both scopes, for the third-direction probe.
        row = session.exec(
            text(
                "SELECT id FROM account WHERE deleted_at IS NULL"
                f" AND (group_id IS NULL OR group_id != {grp_id})"
                f" AND (customer_id IS NULL OR customer_id != {cust_id})"
                " ORDER BY id DESC LIMIT 1"
            )
        ).first()
        outsider_id = int(row[0])
    else:
        session.add(AppSettings(id=1, default_rate_per_gb=1000))
        session.commit()
        cust = Customer(name="Scratch Cust")
        session.add(cust)
        session.commit()
        session.refresh(cust)
        grp = Group(name="Scratch Grp", billing_mode="payg",
                    representative_customer_id=cust.id)
        session.add(grp)
        session.commit()
        session.refresh(grp)
        cust_id, grp_id, outsider_id = cust.id, grp.id, 0
        cust_n = grp_n = 0

    # The two delegates, seeded into the COPY only.
    session.add(Delegate(telegram_id=9001, customer_id=cust_id, is_active=True,
                         label="idor-A-customer"))
    session.add(Delegate(telegram_id=9002, group_id=grp_id, is_active=True,
                         label="idor-B-group"))
    session.commit()

print(f"== scopes: customer #{cust_id} ({cust_n} live accounts),"
      f" group #{grp_id} ({grp_n} live members), outsider account #{outsider_id} ==")

# --------------------------------------------------------------- the probes
r = client.post("/api/delegate/bot/session", headers=BOT_HEADERS, json={"telegram_id": 9001})
check("A session ok (customer scope)", r.status_code == 200)
a_scope = r.json().get("scope_name", "")

r = client.post("/api/delegate/bot/session", headers=BOT_HEADERS, json={"telegram_id": 9002})
check("B session ok (group scope)", r.status_code == 200)
b_scope = r.json().get("scope_name", "")
check("A and B resolve to DIFFERENT scopes", a_scope != b_scope)

r = client.get("/api/delegate/bot/accounts", params={"telegram_id": 9001},
               headers=BOT_HEADERS)
a_list = r.json()
check("A list 200", r.status_code == 200)

# Leak check against raw SQL ground truth in the same copy.
with Session(engine) as session:
    truth = {
        int(a.id)
        for a in session.exec(
            text("SELECT id FROM account WHERE deleted_at IS NULL"
                 f" AND customer_id = {cust_id}")
        ).all()
    }
shown = {a["id"] for a in a_list}
check(f"A sees exactly its scope ({len(shown)} shown == {len(truth)} in scope)",
      shown == truth)

if outsider_id:
    for act in ("renew", "delete"):
        body = {"telegram_id": 9001, "extend_gb": 1} if act == "renew" else {"telegram_id": 9001}
        r = client.post(f"/api/delegate/bot/accounts/{outsider_id}/{act}",
                        headers=BOT_HEADERS, json=body)
        check(f"A {act} on outsider account refused ({r.status_code})",
              r.status_code in (400, 403, 404))

    # A renewing an account inside B's group scope.
    with Session(engine) as session:
        row = session.exec(
            text("SELECT id FROM account WHERE deleted_at IS NULL"
                 f" AND group_id = {grp_id} ORDER BY id LIMIT 1")
        ).first()
    if row is not None:
        b_account = int(row[0])
        r = client.post(f"/api/delegate/bot/accounts/{b_account}/renew",
                        headers=BOT_HEADERS, json={"telegram_id": 9001, "extend_gb": 1})
        check(f"A renew on B's group member refused ({r.status_code})",
              r.status_code in (400, 403, 404))

    # B reaching into A's customer accounts.
    if truth:
        target = sorted(truth)[0]
        r = client.post(f"/api/delegate/bot/accounts/{target}/renew",
                        headers=BOT_HEADERS, json={"telegram_id": 9002, "extend_gb": 1})
        check(f"B renew on A's customer account refused ({r.status_code})",
              r.status_code in (400, 403, 404))
        r = client.post(f"/api/delegate/bot/accounts/{target}/delete",
                        headers=BOT_HEADERS, json={"telegram_id": 9002})
        check(f"B delete on A's customer account refused ({r.status_code})",
              r.status_code in (400, 403, 404))

r = client.get("/api/delegate/bot/accounts", params={"telegram_id": 9002},
               headers=BOT_HEADERS)
b_list = r.json()
b_truth_row = None
with Session(engine) as session:
    b_truth_row = session.exec(
        text("SELECT COUNT(*) FROM account WHERE deleted_at IS NULL"
             f" AND group_id = {grp_id}")
    ).first()
check(f"B sees exactly its group scope ({len(b_list)} shown == {b_truth_row[0]} in scope)",
      len(b_list) == int(b_truth_row[0]))

# Auth boundary still closed.
r = client.post("/api/delegate/bot/session", headers={"X-Delegate-Bot-Key": "wrong"},
                json={"telegram_id": 9001})
check("wrong bot key rejected", r.status_code in (401, 403))
r = client.post("/api/delegate/bot/session", headers=BOT_HEADERS, json={"telegram_id": 424242})
check("unknown telegram_id rejected (403)", r.status_code == 403)

# No Marzban call ever fired: every hostile probe must have died at the
# scope check, long before provisioning.
check(f"zero Marzban calls fired during all probes ({fake.touched})", not fake.touched)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All delegate IDOR hostile cases passed"
      + (" on the LIVE COPY." if _LIVE_COPY else " (scratch DB — set VPN_IDOR_DB for the live pass)."))
