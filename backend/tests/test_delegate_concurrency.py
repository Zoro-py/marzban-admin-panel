"""Delegate charge endpoints must be money-consistent under concurrency
(2026-09-27 audit, brief F5): /api/delegate/bot/accounts and .../renew post
real LedgerEntry charges but ran OUTSIDE serialise_billing — every other
money-moving endpoint (settle/reset/mark-paid) takes the billing lock.

The concrete shape: two concurrent renew calls for the same account both read
the same pre-renew data_limit, both compute and send the SAME absolute target
to Marzban (modify_user is a PUT, not a delta — the account is extended once)
but BOTH post a ledger charge → two charges for one extension, i.e. invented
debt. Under the billing lock the two requests serialise: the second re-reads
the committed first, so every charge corresponds to a real extension —
money-consistent whatever the caller's intent (the bot's own _RENEW_IN_FLIGHT
guard stays the human-double-tap layer).

Fires two truly concurrent renews (one event loop, ASGI transport) and asserts
the charge/extension invariant: final data_limit == base + extend_gb ×
#charges. Plain `python -m tests.test_delegate_concurrency` from `backend/`.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="delegate_conc_test_") ) / "test.db"
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

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import marzban_client as marzban_module  # noqa: E402
from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AppSettings, Customer, Delegate, LedgerEntry  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class FakeMarzban:
    """A panel whose modify_user is an absolute SET, like the real one: the
    response's data_limit is whatever the payload carried, so two concurrent
    renews computing the SAME target really do extend the account only once
    on the panel — which is what makes a second charge an invention."""

    def __init__(self):
        self.panel: dict[str, dict] = {}

    async def create_user(self, payload: dict) -> dict:
        user = dict(payload)
        user["used_traffic"] = 0
        user["lifetime_used_traffic"] = 0
        self.panel[payload["username"]] = user
        return user

    async def modify_user(self, username: str, payload: dict) -> dict:
        # A realistic in-flight window: without the billing lock, BOTH
        # requests complete their read-compute phase (same pre-renew base)
        # before either commits — the exact interleaving that double-charges
        # one extension. With the lock, the second request's read happens
        # after the first one's commit.
        await asyncio.sleep(0.05)
        user = self.panel[username]
        user.update({k: v for k, v in payload.items() if k in ("data_limit", "expire", "status")})
        return dict(user)

    async def list_all_users(self) -> list[dict]:
        return [dict(u, username=name) for name, u in self.panel.items()]


fake = FakeMarzban()
marzban_module.marzban_client.create_user = fake.create_user
marzban_module.marzban_client.modify_user = fake.modify_user
marzban_module.marzban_client.list_all_users = fake.list_all_users

app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)
BOT_HEADERS = {"X-Delegate-Bot-Key": "test-delegate-key"}
GB = 1024 ** 3
RATE = 1000.0

with Session(engine) as session:
    session.add(AppSettings(id=1, default_rate_per_gb=RATE))
    customer = Customer(name="Delegate Concurrency Customer")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    customer_id = customer.id

r = client.post("/api/delegate", json={"customer_id": customer_id, "telegram_id": 9101,
                                       "credit_limit": 1000000, "daily_create_cap": 20})
check("delegate created", r.status_code == 200, str(r.status_code))
r = client.post("/api/delegate/bot/accounts", headers=BOT_HEADERS,
                json={"telegram_id": 9101, "data_limit_gb": 10})
check("account created", r.status_code == 200, str(r.status_code))
account_id = r.json()["id"]
base_limit = r.json()["data_limit"]


async def concurrent_double_renew() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:

        async def hit():
            return await c.post(
                f"/api/delegate/bot/accounts/{account_id}/renew",
                headers=BOT_HEADERS,
                json={"telegram_id": 9101, "extend_gb": 5},
            )

        r1, r2 = await asyncio.gather(hit(), hit())

    codes = sorted([r1.status_code, r2.status_code])
    check("both concurrent renews got a real response", all(c in (200, 400) for c in codes),
          f"codes={codes}")

    with Session(engine) as session:
        charges = session.exec(
            select(LedgerEntry).where(
                LedgerEntry.account_id == account_id,
                LedgerEntry.type == "charge",
                LedgerEntry.source == "delegate",
                LedgerEntry.gb_amount == 5.0,  # renew charges only (create = 10GB)
            )
        ).all()
        acc = session.get(__import__("app.models", fromlist=["Account"]).Account, account_id)
        n_charges = len(charges)
        final_limit = acc.data_limit or 0
        expected = base_limit + 5 * GB * n_charges
        check("charge/extension invariant: final data_limit == base + extend_gb × #charges",
              abs(final_limit - expected) < 1,
              f"charges={n_charges}, final={final_limit / GB:g}GB, expected={expected / GB:g}GB")
        check("every delegate charge carries the same gb_amount as its extension",
              all(abs(c.gb_amount - 5.0) < 0.001 for c in charges),
              f"gb_amounts={[c.gb_amount for c in charges]}")


asyncio.run(concurrent_double_renew())

print()
if failures:
    print(f"RESULT: {len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("RESULT: delegate concurrency checks OK")
