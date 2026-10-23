"""POST /api/accounts/{id}/adjust with an `Idempotency-Key` (F13): same key =
ONE mutation (one Marzban modify, one AccountEvent) no matter how many times
the request replays; a different body on the same key is refused (409); no
key = behavior unchanged, every request applies. And POST /api/ledger — the
endpoint the mechanism was extracted FROM — behaves exactly as before.

Plain `python tests/test_adjust_idempotency.py` from `backend/`, like
tests/test_ledger_idempotency.py (which keeps covering the ledger side in
depth; this file pins the shared-registry contract end to end).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="adjust_idem_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import marzban_client as marzban_module  # noqa: E402
from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, AccountEvent, Customer, LedgerEntry  # noqa: E402

init_db()
app.dependency_overrides[require_auth] = lambda: "test-admin"

failures: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual == expected:
        print(f"[OK] {label}")
    else:
        print(f"[FAIL] {label}\n       expected: {expected!r}\n       actual:   {actual!r}")
        failures.append(label)


class FakeMarzban:
    """A panel that counts modify calls — the mutation the key must collapse."""

    def __init__(self):
        self.modifies: list[tuple[str, dict]] = []
        self.panel: dict[str, dict] = {}

    async def modify_user(self, username: str, payload: dict) -> dict:
        user = self.panel.setdefault(username, {"username": username, "status": "active"})
        user.update(payload)
        self.modifies.append((username, dict(payload)))
        return dict(user)

    async def get_user(self, username: str):
        return self.panel.get(username)

    async def create_user(self, payload: dict) -> dict:
        user = {"username": payload["username"], "status": "active", **payload}
        self.panel[payload["username"]] = user
        return dict(user)


fake = FakeMarzban()
marzban_module.marzban_client.modify_user = fake.modify_user
marzban_module.marzban_client.get_user = fake.get_user
marzban_module.marzban_client.create_user = fake.create_user

client = TestClient(app)

with Session(engine) as s:
    account = Account(marzban_username="adjust_idem", status="active",
                      data_limit=10 * 1024 ** 3)
    s.add(account)
    s.commit()
    aid = account.id


def adjust_events() -> int:
    with Session(engine) as s:
        return len(s.exec(select(AccountEvent).where(
            AccountEvent.account_id == aid,
            AccountEvent.action == "adjust",
        )).all())


body = {"extend_days": 7}

r1 = client.post(f"/api/accounts/{aid}/adjust", json=body, headers={"Idempotency-Key": "adj-1"})
r2 = client.post(f"/api/accounts/{aid}/adjust", json=body, headers={"Idempotency-Key": "adj-1"})
check("same key twice: both 200", (r1.status_code, r2.status_code), (200, 200))
check("same key twice: the SAME account record comes back", r1.json()["id"] == r2.json()["id"], True)
check("same key twice: ONE Marzban modify", len(fake.modifies), 1)
check("same key twice: ONE adjust event", adjust_events(), 1)

r3 = client.post(f"/api/accounts/{aid}/adjust", json={"extend_days": 14},
                 headers={"Idempotency-Key": "adj-1"})
check("same key with a DIFFERENT body is refused (409)", r3.status_code, 409)
check("the refusal applied nothing", len(fake.modifies), 1)

r4 = client.post(f"/api/accounts/{aid}/adjust", json={"extend_days": 7})
r5 = client.post(f"/api/accounts/{aid}/adjust", json={"extend_days": 7})
check("no key: both apply, as before", (r4.status_code, r5.status_code), (200, 200))
check("no key: two Marzban modifies", len(fake.modifies), 3)
check("no key: two adjust events", adjust_events(), 3)

r6 = client.post(f"/api/accounts/{aid}/adjust", json={"extend_days": 99999},
                 headers={"Idempotency-Key": "adj-bad"})
check("an invalid request is refused before the endpoint runs (422)", r6.status_code, 422)
r7 = client.post(f"/api/accounts/{aid}/adjust", json=body, headers={"Idempotency-Key": "adj-bad"})
check("a rejected request did not burn its key", (r7.status_code, len(fake.modifies)), (200, 4))

# ── and the ledger endpoint behaves EXACTLY as before the extraction ──────

with Session(engine) as s:
    c = Customer(name="Adjust Idem Ledger Probe")
    s.add(c)
    s.commit()
    s.refresh(c)
    cid = c.id


def ledger_rows() -> int:
    with Session(engine) as s:
        return len(s.exec(select(LedgerEntry).where(LedgerEntry.customer_id == cid)).all())


lbody = {"type": "credit", "amount": 50000, "customer_id": cid, "note": "payment"}
l1 = client.post("/api/ledger", json=lbody, headers={"Idempotency-Key": "lg-1"})
l2 = client.post("/api/ledger", json=lbody, headers={"Idempotency-Key": "lg-1"})
check("ledger same key twice: both 200, SAME entry", (l1.status_code, l2.status_code), (200, 200))
check("ledger same key twice: one row", ledger_rows(), 1)
l3 = client.post("/api/ledger", json={**lbody, "amount": 1}, headers={"Idempotency-Key": "lg-1"})
check("ledger same key, different body: 409", l3.status_code, 409)
client.post("/api/ledger", json=lbody)
client.post("/api/ledger", json=lbody)
check("ledger no key: every request appends", ledger_rows(), 3)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All adjust-idempotency cases passed.")
