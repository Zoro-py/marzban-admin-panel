"""Checks for the GB figures added to GET /api/ledger/balance — gb_charged
(sum of what was billed/sold in the window), gb_consumed (sum of the actual
usage attributed to those charges) and gb_pending (live, open-cycle usage no
charge has attributed yet) — across all three scopes.

Plain `python -m tests.test_balance_gb` from `backend/`, same harness shape
as tests/test_balance_since.py. Marzban is faked (same pattern as
tests/test_delegate_smoke.py) so settle/reset endpoints can run for real —
the numbers are verified against hand-reconstructed math, not just
self-consistency.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="balance_gb_test_")) / "test.db"
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
    Group,
    LedgerEntry,
    LedgerType,
    utcnow,
)

init_db()

# The charge sites read the dashboard-wide default rate; without this row
# every figure would compute to 0 and no charge would post at all.
with Session(engine) as session:
    session.add(AppSettings(id=1, default_rate_per_gb=5000))
    session.commit()

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


class FakeMarzban:
    """Records resets; echoes back a reset meter (used_traffic 0)."""

    def __init__(self):
        self.resets: list[str] = []

    async def reset_user(self, username: str) -> dict:
        self.resets.append(username)
        return {"username": username, "used_traffic": 0, "status": "active"}


fake = FakeMarzban()
marzban_module.marzban_client.reset_user = fake.reset_user

app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)

now = utcnow()
GB = 1024**3


def gb(x: float) -> int:
    return int(x * GB)


# ══════════════════════════════════ scenario 1: the operator's own example ════
# Two prepay accounts under one customer, one 20GB package each, settled once
# each after being used 18GB and 15GB. Expected: 40 GB charged, 33 GB consumed.
with Session(engine) as session:
    cust = Customer(name="GB Test Customer")
    session.add(cust)
    session.commit()
    session.refresh(cust)
    cust_id = cust.id

    a1 = Account(
        marzban_username="gb-one", customer_id=cust_id, billing_mode=BillingMode.prepay,
        data_limit=gb(20), billed_data_limit=0, used_traffic=gb(18), usage_baseline=0,
        usage_baseline_at=now - timedelta(days=20),
    )
    a2 = Account(
        marzban_username="gb-two", customer_id=cust_id, billing_mode=BillingMode.prepay,
        data_limit=gb(20), billed_data_limit=0, used_traffic=gb(15), usage_baseline=0,
        usage_baseline_at=now - timedelta(days=20),
    )
    session.add(a1)
    session.add(a2)
    session.commit()
    session.refresh(a1)
    session.refresh(a2)
    a1_id, a2_id = a1.id, a2.id

r1 = client.post(f"/api/accounts/{a1_id}/settle")
r2 = client.post(f"/api/accounts/{a2_id}/settle")
check("prepay settle of a 20GB package charges 100000 (20 x 5000)", r1.json()["charged_amount"] == 100_000.0)

bal = client.get("/api/ledger/balance", params={"customer_id": cust_id}).json()
check("operator example: 40 GB charged for the customer", bal["gb_charged"] == 40.0)
check("operator example: 33 GB consumed for the customer", bal["gb_consumed"] == 33.0)
check("money still nets correctly alongside GB (200000 owed)", bal["balance"] == 200_000.0)
check("charged Toman = gross billed (200000, not netted against credits)", bal["charged_amount"] == 200_000.0)
check("consumed Toman = consumed GB x each charge's own rate (33 x 5000)", bal["consumed_amount"] == 165_000.0)

bal1 = client.get("/api/ledger/balance", params={"account_id": a1_id}).json()
check("account scope: 20 GB charged / 18 GB consumed", bal1["gb_charged"] == 20.0 and bal1["gb_consumed"] == 18.0)
check("account scope: nothing pending after a full settle", bal1["gb_pending"] == 0.0)

# ══════════════════════════════════════════════ scenario 2: piecemeal billing ════
# The same account keeps running: +2GB accrued after the settle, operator tops
# the package up to 40GB and settles again. The second charge must attribute
# ONLY the new 2GB — not the 18 the first charge already took.
with Session(engine) as session:
    a = session.get(Account, a1_id)
    a.used_traffic = gb(20)   # 2 more GB burned since the settle
    a.data_limit = gb(40)     # operator topped the package up
    session.add(a)
    session.commit()

r3 = client.post(f"/api/accounts/{a1_id}/settle").json()
check("second settle charges only the new 20GB remainder", r3["charged_amount"] == 100_000.0)
bal1 = client.get("/api/ledger/balance", params={"account_id": a1_id}).json()
check("piecemeal: 40 GB charged total (20 + 20)", bal1["gb_charged"] == 40.0)
check("piecemeal: consumed 20 (18 + 2), NOT 38 — no overlap", bal1["gb_consumed"] == 20.0)
check("piecemeal: nothing pending after the second settle", bal1["gb_pending"] == 0.0)

# ═══════════════════════════════════════════════════ scenario 3: open cycle ════
# Fresh usage with no charge yet shows as pending, not lost.
with Session(engine) as session:
    a = session.get(Account, a1_id)
    a.used_traffic = gb(25)   # 5GB into the 40GB package since the last settle
    session.add(a)
    session.commit()
bal1 = client.get("/api/ledger/balance", params={"account_id": a1_id}).json()
check("open cycle: 5 GB accruing shows as gb_pending", bal1["gb_pending"] == 5.0)
check("open cycle: posted gb figures unchanged by pending usage", bal1["gb_charged"] == 40.0 and bal1["gb_consumed"] == 20.0)

# ═════════════════════════════════════════════════════ scenario 4: payg ════════
# Two payg cycles: settle rolls the baseline each time, so the second cycle's
# consumed is ONLY the new usage — and equals the charged GB by definition.
with Session(engine) as session:
    payg = Account(
        marzban_username="gb-payg", customer_id=cust_id, billing_mode=BillingMode.payg,
        data_limit=gb(50), used_traffic=gb(12), usage_baseline=0,
        usage_baseline_at=now - timedelta(days=5),
    )
    session.add(payg)
    session.commit()
    session.refresh(payg)
    payg_id = payg.id

client.post(f"/api/accounts/{payg_id}/settle")
with Session(engine) as session:
    p = session.get(Account, payg_id)
    p.used_traffic = gb(3)  # 3GB into the second cycle (the meter was reset to 0 at settle)
    session.add(p)
    session.commit()
client.post(f"/api/accounts/{payg_id}/settle")
balp = client.get("/api/ledger/balance", params={"account_id": payg_id}).json()
check("payg: charged equals consumed per cycle (12 + 3)", balp["gb_charged"] == 15.0 and balp["gb_consumed"] == 15.0)

# ═══════════════════════════════════════════════════ scenario 5: reset ════════
# A prepay reset (charge auto-computed) carries the package GB and attributes
# the accrued consumption; the meter is zeroed either way.
with Session(engine) as session:
    rp = Account(
        marzban_username="gb-reset", customer_id=cust_id, billing_mode=BillingMode.prepay,
        data_limit=gb(10), billed_data_limit=0, used_traffic=gb(7), usage_baseline=0,
        usage_baseline_at=now - timedelta(days=3),
    )
    session.add(rp)
    session.commit()
    session.refresh(rp)
    rp_id = rp.id

client.post(f"/api/accounts/{rp_id}/reset", json={})
balr = client.get("/api/ledger/balance", params={"account_id": rp_id}).json()
check("prepay reset: 10 GB charged (package size)", balr["gb_charged"] == 10.0)
check("prepay reset: 7 GB consumed attributed even though the meter was zeroed", balr["gb_consumed"] == 7.0)

# Operator-entered reset amount maps to no honest GB — but consumption is
# still attributed (the meter was zeroed regardless).
with Session(engine) as session:
    rp2 = Account(
        marzban_username="gb-reset2", customer_id=cust_id, billing_mode=BillingMode.prepay,
        data_limit=gb(10), billed_data_limit=0, used_traffic=gb(6), usage_baseline=0,
        usage_baseline_at=now - timedelta(days=3),
    )
    session.add(rp2)
    session.commit()
    session.refresh(rp2)
    rp2_id = rp2.id

client.post(f"/api/accounts/{rp2_id}/reset", json={"charge_amount": 12345})
balr2 = client.get("/api/ledger/balance", params={"account_id": rp2_id}).json()
check("explicit-amount reset: gb_charged unknown (None)", balr2["gb_charged"] is None)
check("explicit-amount reset: consumption still attributed (6 GB)", balr2["gb_consumed"] == 6.0)

# ═══════════════════════════════════════════ scenario 6: group roll-up ════════
# A prepay group with two members settled together — one charge per member,
# summed at group scope. (No Marzban call: prepay group settle doesn't reset.)
with Session(engine) as session:
    rep = Customer(name="GB Group Rep")
    session.add(rep)
    session.commit()
    session.refresh(rep)
    rep_id = rep.id
    grp = Group(name="GB Test Group", representative_customer_id=rep.id, billing_mode=BillingMode.prepay)
    session.add(grp)
    session.commit()
    session.refresh(grp)
    grp_id = grp.id
    m1 = Account(
        marzban_username="gb-g1", group_id=grp.id, billing_mode=BillingMode.prepay,
        data_limit=gb(30), billed_data_limit=0, used_traffic=gb(22), usage_baseline=0,
        usage_baseline_at=now - timedelta(days=10),
    )
    m2 = Account(
        marzban_username="gb-g2", group_id=grp.id, billing_mode=BillingMode.prepay,
        data_limit=gb(10), billed_data_limit=0, used_traffic=gb(1), usage_baseline=0,
        usage_baseline_at=now - timedelta(days=10),
    )
    session.add(m1)
    session.add(m2)
    session.commit()

client.post(f"/api/groups/{grp_id}/settle")
balg = client.get("/api/ledger/balance", params={"group_id": grp_id}).json()
check("group scope: 40 GB charged across members", balg["gb_charged"] == 40.0)
check("group scope: 23 GB consumed across members (22 + 1)", balg["gb_consumed"] == 23.0)
balrep = client.get("/api/ledger/balance", params={"customer_id": rep_id}).json()
check("representative customer sees the group's GB through the roll-up", balrep["gb_charged"] == 40.0 and balrep["gb_consumed"] == 23.0)

# ═════════════════════════════════ scenario 7: legacy rows + since window ═════
# A charge with no GB data (predating the feature / manual money-only) leaves
# the scope "unknown" on its own, and only stretches a mixed sum by what it
# actually knows. The `since` filter applies to GB exactly like money.
with Session(engine) as session:
    legacy_cust = Customer(name="GB Legacy Customer")
    session.add(legacy_cust)
    session.commit()
    session.refresh(legacy_cust)
    legacy_id = legacy_cust.id
    old_date = now - timedelta(days=40)
    session.add(LedgerEntry(type=LedgerType.charge, amount=50_000, customer_id=legacy_id, date=old_date))
    session.commit()

ball = client.get("/api/ledger/balance", params={"customer_id": legacy_id}).json()
check("legacy-only window: gb_charged is None (unknown), not 0", ball["gb_charged"] is None)
check("legacy-only window: gb_consumed is None", ball["gb_consumed"] is None)
check("legacy-only window: money still reported", ball["balance"] == 50_000.0)
check("legacy-only window: charged Toman STILL known (plain money)", ball["charged_amount"] == 50_000.0)
check("legacy-only window: consumed Toman unknown", ball["consumed_amount"] is None)

with Session(engine) as session:
    session.add(LedgerEntry(
        type=LedgerType.charge, amount=20_000, customer_id=legacy_id,
        date=now - timedelta(days=1), gb_amount=4.0, consumed_gb=3.5,
    ))
    session.commit()
ball = client.get("/api/ledger/balance", params={"customer_id": legacy_id}).json()
check("mixed window: known GB summed (4 charged), unknown rows skipped", ball["gb_charged"] == 4.0)
check("mixed window: known consumption summed (3.5)", ball["gb_consumed"] == 3.5)

# Same mixed-window guarantee at ACCOUNT scope (a single account with one
# legacy GB-less charge and one known-GB charge — the exact shape account 13
# took after its one-off gb_amount backfill).
with Session(engine) as session:
    mix_acct = Account(
        marzban_username="gb-mix", customer_id=legacy_id, billing_mode=BillingMode.prepay,
        data_limit=5 * 1024**3, billed_data_limit=0, used_traffic=0,
        usage_baseline=0, usage_baseline_at=utcnow(),
    )
    session.add(mix_acct)
    session.commit()
    session.refresh(mix_acct)
    mix_id = mix_acct.id
    session.add(LedgerEntry(
        type=LedgerType.charge, amount=10_000, account_id=mix_id, customer_id=legacy_id,
        date=now - timedelta(days=5),
    ))  # legacy: no GB columns
    session.add(LedgerEntry(
        type=LedgerType.charge, amount=30_000, account_id=mix_id, customer_id=legacy_id,
        date=now - timedelta(days=4), gb_amount=6.0, consumed_gb=4.5, consumed_amount=22_500,
    ))
    session.commit()
balm = client.get("/api/ledger/balance", params={"account_id": mix_id}).json()
check("account-scope mixed window: known GB summed (6 charged)", balm["gb_charged"] == 6.0)
check("account-scope mixed window: known consumption summed (4.5)", balm["gb_consumed"] == 4.5)
check("account-scope mixed window: charged Toman covers BOTH rows (40k)", balm["charged_amount"] == 40_000.0)
check("account-scope mixed window: consumed Toman from the known row only", balm["consumed_amount"] == 22_500.0)

# since: only the manual 4GB charge is inside the window -> same figures; the
# legacy 50k charge drops out of the money balance.
balw = client.get("/api/ledger/balance", params={"customer_id": legacy_id, "since": (now - timedelta(days=2)).replace(tzinfo=timezone.utc).isoformat()}).json()
check("since window: gb figures respect it (only the 4GB charge is inside)", balw["gb_charged"] == 4.0 and balw["gb_consumed"] == 3.5)
check("since window: money respects it too (50k legacy charge excluded)", balw["balance"] == 20_000.0)

# ════════════════════════════════ scenario 8: manual entry with caller GB ═════
r = client.post("/api/ledger", json={
    "type": "charge", "amount": 600_000, "customer_id": cust_id,
    "gb_amount": 120.0, "note": "Recovery charge with known GB",
})
check("manual ledger entry accepts gb_amount", r.status_code == 200 and r.json()["gb_amount"] == 120.0)
check("manual entry does not invent consumed_gb", r.json()["consumed_gb"] is None)
# By this point customer 1's cumulative posted GB:
#   charged: a1 40 (20+20) + a2 20 + payg 15 (12+3) + reset 10 + manual 120 = 205
#   consumed: a1 20 (18+2) + a2 15 + payg 15 + reset 7 + reset2 6       = 63
#   (the explicit-amount reset contributes money but no known charged GB)
balc = client.get("/api/ledger/balance", params={"customer_id": cust_id}).json()
check("manual entry's GB flows into the scope sum (205 total)", balc["gb_charged"] == 205.0)
check("manual entry's consumption stays out of the sum (63 total)", balc["gb_consumed"] == 63.0)

# gb_charged never counts credits
client.post("/api/ledger", json={"type": "credit", "amount": 5_000, "customer_id": cust_id, "gb_amount": 9.0})
balc = client.get("/api/ledger/balance", params={"customer_id": cust_id}).json()
check("a credit carrying GB is ignored by the charged sum", balc["gb_charged"] == 205.0)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All balance-GB cases passed.")
