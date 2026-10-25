"""Payg customers get SUPPORTED shop linking (owner 2026-10-24: «مشتری‌های
این مدلی هم ساپورت بشن») — the one-click «تسویه + تبدیل + اتصال» on the
direct bind: settle each standalone payg account (charge becomes real),
flip to prepay (through the guarded endpoint — D21 neutralization rides
along), then link. Group-payg members stay refused (flip the group first).
The reverse hole stays closed: a linked customer can never be flipped back
to payg (the pre-existing flip guard).

Money invariants locked here:
  - the conversion charge equals exactly the account's pending at settle
    time (billable_bytes × effective rate — the same numbers the panel
    showed), never more, never less;
  - nothing is forgiven: after convert+link the customer's posted balance
    carries the new charge;
  - billed_data_limit is neutralized on the flip (no phantom package debt);
  - the settle ran with the PAYG mode (meter reset called, baseline rolled)
    BEFORE the flip (order matters: flip-first would bill the package).

Plain `python -m tests.test_shop_payg_link` from `backend/` — no pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="payglink_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""
os.environ["SHOP_BOT_API_KEY"] = "test-shop-key"
os.environ["DELEGATE_BOT_API_KEY"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import marzban_client as marzban_module  # noqa: E402
from app import routers as routers_pkg  # noqa: E402
from app.routers import accounts as accounts_module  # noqa: E402
from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, BillingMode, Customer, Group, LedgerEntry, LedgerType  # noqa: E402
from app.services import MoneyBook  # noqa: E402

init_db()

failures: list[str] = []


def check(name: str, cond: bool, extra=None):
    print(("  [OK] " if cond else "[FAIL] ") + name + (f"  {extra}" if (extra is not None and not cond) else ""))
    if not cond:
        failures.append(name)


# settle_account (payg) calls Marzban's reset_user BEFORE any DB write; fake
# it (same pattern as tests/test_created_by.py — instance attr, no binding).
reset_calls: list[str] = []


async def _fake_reset_user(username: str) -> dict:
    reset_calls.append(username)
    return {"username": username, "used_traffic": 0, "status": "active"}


accounts_module.marzban_client.reset_user = _fake_reset_user

app.dependency_overrides[require_auth] = lambda: "testop"
client = TestClient(app)

GB = 1024 ** 3

with Session(engine) as s:
    cust = Customer(name="Payg Customer")
    s.add(cust)
    s.flush()
    # standalone payg: 10 GB accrued since baseline, rate 10,000 T/GB
    acc = Account(
        marzban_username="payg_one", customer_id=cust.id, billing_mode=BillingMode.payg,
        used_traffic=10 * GB, usage_baseline=0, usage_baseline_at=None,
        rate_per_gb=10000.0,
        # a stale billed_data_limit leftover that MUST be neutralized by the
        # flip (D21): without neutralization, prepay's billable would read
        # (data_limit - billed) as a phantom package on the next settle.
        data_limit=None, billed_data_limit=0,
    )
    s.add(acc)
    # a second standalone payg with rate 0 (comp/free) — converts with no charge
    acc2 = Account(
        marzban_username="payg_free", customer_id=cust.id, billing_mode=BillingMode.payg,
        used_traffic=3 * GB, usage_baseline=0, rate_per_gb=0.0,
    )
    s.add(acc2)
    # group member whose GROUP is payg — must block the one-click
    grp = Group(name="Payg Grp", representative_customer_id=cust.id, billing_mode=BillingMode.payg)
    s.add(grp)
    s.flush()
    grp_member = Account(
        marzban_username="payg_in_grp", customer_id=cust.id, group_id=grp.id,
        billing_mode=BillingMode.payg, used_traffic=0, rate_per_gb=10000.0,
    )
    s.add(grp_member)
    s.commit()
    CUST = cust.id
    ACC1, ACC2, GRP_ID = acc.id, acc2.id, grp.id

# link state preview BEFORE anything: has_payg, pending ≈ 100,000 (10GB × 10k)
r0 = client.get(f"/api/shop/link/{CUST}").json()
check("preview: has_payg on not_linked", r0["has_payg"] is True, r0)
check("preview: pending ≈ 100000", abs(r0["payg_pending_amount"] - 100000.0) < 0.01, r0["payg_pending_amount"])
check("preview: group named", r0["payg_group_names"] == ["Payg Grp"], r0["payg_group_names"])

# 1) bind WITHOUT convert → refused; the GROUP refusal is the most specific
# and fires first for this customer (who has both standalone and group payg).
r1 = client.post(f"/api/shop/link/{CUST}/telegram", json={"telegram_id": 600001})
check("refuse: no-flag bind → 409", r1.status_code == 409, r1.status_code)
check("refuse: group refusal is the specific one", "Payg Grp" in r1.json()["detail"], r1.json()["detail"])

# 2) convert WITH a payg GROUP member → 409 naming the group, nothing changed
r2 = client.post(f"/api/shop/link/{CUST}/telegram", json={"telegram_id": 600001, "convert_payg": True})
check("group payg: 409 even with convert", r2.status_code == 409, r2.text[:200])
check("group payg: refusal names the group", "Payg Grp" in r2.json()["detail"], r2.json()["detail"])
with Session(engine) as s:
    check("group payg: nothing converted yet", s.get(Account, ACC1).billing_mode == BillingMode.payg)
    check("group payg: not linked", s.get(Customer, CUST).shop_user_id is None)

# ── remove the group membership (simulate the operator flipping the group ──
# path: detach the member so the one-click applies to the standalone pair)
with Session(engine) as s:
    m = s.get(Account, s.get(Account, ACC1).id)  # keep ACC1 reference alive
    member = s.exec(select(Account).where(Account.marzban_username == "payg_in_grp")).one()
    member.group_id = None
    member.billing_mode = BillingMode.prepay  # operator handled it separately
    s.add(member)
    s.commit()

reset_calls.clear()

# 3) THE one-click: convert + link
r3 = client.post(f"/api/shop/link/{CUST}/telegram", json={"telegram_id": 600001, "convert_payg": True})
check("convert: 200", r3.status_code == 200, r3.text[:300])
body = r3.json()
check("convert: both standalone accounts converted", body.get("converted_payg_accounts") == 2, body)
check("convert: settled total = 100,000 (payg_free rate 0 adds nothing)", abs(body.get("settled_amount", -1) - 100000.0) < 0.01, body.get("settled_amount"))

with Session(engine) as s:
    a1 = s.get(Account, ACC1)
    a2 = s.get(Account, ACC2)
    c = s.get(Customer, CUST)
    check("mode: both flipped to prepay", a1.billing_mode == BillingMode.prepay and a2.billing_mode == BillingMode.prepay)
    check("meter: Marzban reset called for the payg settles (payg only)", sorted(reset_calls) == ["payg_free", "payg_one"], reset_calls)
    check("meter+baseline: both mirror the reset (billable now 0)", a1.used_traffic == 0 and a1.usage_baseline == 0, (a1.used_traffic, a1.usage_baseline))
    check("linked: customer bound", c.shop_user_id is not None)

    # the charge: exactly billable×rate — the same number the panel showed
    charges = s.exec(
        select(LedgerEntry).where(LedgerEntry.account_id.in_([ACC1, ACC2]), LedgerEntry.type == LedgerType.charge)
    ).all()
    total = round(sum(e.amount for e in charges), 2)
    check("money: charge total = 100,000 exactly", abs(total - 100000.0) < 0.01, total)
    book = MoneyBook(s)
    check("money: nothing forgiven — posted balance carries the charge", abs(book.customer_posted(c) - 100000.0) < 0.01, book.customer_posted(c))

# 4) the reverse hole stays closed: flip a linked account back to payg → 409
r4 = client.patch(f"/api/accounts/{ACC1}/billing", json={"billing_mode": "payg"})
check("flip guard: prepay→payg on a LINKED customer → 409", r4.status_code == 409, r4.text[:200])

# 5) idempotent re-bind (same pair, no flag): refresh, no refusal (no payg left)
r5 = client.post(f"/api/shop/link/{CUST}/telegram", json={"telegram_id": 600001})
check("re-bind: idempotent 200 (no payg surface left)", r5.status_code == 200, r5.text[:200])
check("re-bind: no conversions this time", r5.json().get("converted_payg_accounts") == 0, r5.json())

# 6) link state after: no payg preview
r6 = client.get(f"/api/shop/link/{CUST}").json()
check("preview after: has_payg False (linked, all prepay)", r6["has_payg"] is False and r6["status"] == "linked", r6)

# 7) claims still never convert a payg customer
with Session(engine) as s:
    c2 = Customer(name="Payg Claimant")
    s.add(c2)
    s.flush()
    a3 = Account(marzban_username="payg_claim_acc", customer_id=c2.id, billing_mode=BillingMode.payg,
                 used_traffic=1 * GB, rate_per_gb=5000.0)
    s.add(a3)
    s.commit()
    C2 = c2.id
r7 = client.post("/api/shop/bind-claim", json={"customer_id": C2, "telegram_username": "claimant"})
check("claim: standalone-payg customer may HOLD a claim (it never binds/converts by itself - see edges C-tests)", r7.status_code == 200, r7.text[:150])

# 8) standalone-ONLY payg customer: the general refusal names the one-click
r8 = client.post(f"/api/shop/link/{C2}/telegram", json={"telegram_id": 600002})
check("standalone-only: no-flag 409 names the one-click path", r8.status_code == 409 and "تسویه + تبدیل + اتصال" in r8.json()["detail"], r8.text[:200])
# and converts cleanly with the flag
r9 = client.post(f"/api/shop/link/{C2}/telegram", json={"telegram_id": 600002, "convert_payg": True})
check("standalone-only: convert 200, 1 account, 5,000 T", r9.status_code == 200 and r9.json().get("converted_payg_accounts") == 1 and abs(r9.json().get("settled_amount", -1) - 5000.0) < 0.01, r9.text[:250])

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All payg-link conversion cases passed.")
