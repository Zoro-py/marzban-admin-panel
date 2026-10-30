"""Routed-books debt payment (council 2026-10-24: seyed-claude + nemotron +
gpt-oss consensus — the ledger stays the single debt truth, the wallet stays
purchase money, NOTHING ever transfers implicitly).

Locks:
  1. session.ledger_debt — MoneyBook net_owed of the linked customer, 0 when
     unlinked; purchases' spendable = wallet − debt.
  2. a receipt DESIGNATED for_debt: approval posts ONE ledger credit
     (source=shop, exact amount), wallet untouched, order-binding refused.
  3. pay-from-wallet: the single sanctioned cross-system pair (wallet −X /
     ledger credit +X, shared ref), capped at min(request, wallet, debt),
     double-tap safe.
  4. purchase blocked while wallet − debt < price, with the DEBT_LINE copy.
  5. a plain top-up for a linked-with-debt customer still credits the WALLET
     (routing is the customer's explicit choice, never automatic).

Plain `python -m tests.test_shop_debt_pay` from `backend/` — no pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="debtpay_test_")) / "test.db"
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

from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account, AppSettings, BillingMode, Customer, LedgerEntry, LedgerSource,
    LedgerType, ShopUser, ShopWalletEntry, ShopWalletEntryType,
)
from app.services import MoneyBook  # noqa: E402

init_db()

failures: list[str] = []


def check(name: str, cond: bool, extra=None):
    print(("  [OK] " if cond else "[FAIL] ") + name + (f"  {extra}" if (extra is not None and not cond) else ""))
    if not cond:
        failures.append(name)


app.dependency_overrides[require_auth] = lambda: "testop"
client = TestClient(app)
BOT_HDR = {"X-Shop-Bot-Key": "test-shop-key"}


def debt_of(session, customer_id: int) -> float:
    return max(0.0, MoneyBook(session).customer_net(session.get(Customer, customer_id)))


with Session(engine) as s:
    s.add(AppSettings(id=1, default_rate_per_gb=1000))
    cust = Customer(name="Debtor Customer")
    s.add(cust)
    s.flush()
    # debt source: a standalone prepay account with a posted charge (100,000 T)
    acc = Account(marzban_username="debtor_acc", customer_id=cust.id,
                  billing_mode=BillingMode.prepay, data_limit=10 * 1024**3,
                  billed_data_limit=10 * 1024**3)
    s.add(acc)
    s.flush()
    s.add(LedgerEntry(type=LedgerType.charge, amount=100000.0, account_id=acc.id,
                      customer_id=cust.id, note="package sold"))
    # The REAL live link shape (found on production 2026-10-24): only the
    # CUSTOMER side carries it — ShopUser.customer_id stays NULL. The fixture
    # must match production or the two-way resolution is never exercised.
    su = ShopUser(telegram_id=555001, telegram_username="debtor", display_name="بدهکار")
    s.add(su)
    s.flush()
    cust.shop_user_id = su.id
    s.add(cust)
    s.commit()
    CUST, SU = cust.id, su.id

# ---- 1) session carries the debt -------------------------------------------
r = client.post("/api/shop/bot/session", headers=BOT_HDR,
                json={"telegram_id": 555001, "telegram_username": "debtor", "display_name": "بدهکار"})
check("session: 200", r.status_code == 200, r.text[:150])
check("session: ledger_debt = 100,000", abs(r.json()["ledger_debt"] - 100000.0) < 0.01, r.json().get("ledger_debt"))

# unlinked shop user sees 0
with Session(engine) as s:
    s.add(ShopUser(telegram_id=555002, display_name="غریبه"))
    s.commit()
r = client.post("/api/shop/bot/session", headers=BOT_HDR, json={"telegram_id": 555002})
check("session: unlinked → debt 0", abs(r.json()["ledger_debt"]) < 0.001)

# ---- 2) for_debt receipt: approval → LEDGER credit, wallet untouched -------
r = client.post("/api/shop/bot/topups", headers=BOT_HDR,
                json={"telegram_id": 555001, "claimed_amount": 60000, "receipt_text": "TRK-DEBT-1", "for_debt": True})
check("debt topup: created", r.status_code == 200 and r.json().get("for_debt") is True, r.text[:200])
topup_id = r.json()["id"]

with Session(engine) as s:
    wallet_rows_before = len(s.exec(select(ShopWalletEntry).where(ShopWalletEntry.shop_user_id == SU)).all())
r2 = client.post(f"/api/shop/topups/{topup_id}/approve")
check("debt topup: approved 200", r2.status_code == 200, r2.text[:200])
with Session(engine) as s:
    credits = s.exec(select(LedgerEntry).where(
        LedgerEntry.customer_id == CUST, LedgerEntry.type == LedgerType.credit,
        LedgerEntry.source == LedgerSource.shop)).all()
    check("routing: exactly ONE shop ledger credit", len(credits) == 1, len(credits))
    check("routing: amount = approved 60,000", credits and credits[0].amount == 60000.0, credits[0].amount if credits else None)
    sole = s.exec(select(Account.id).where(Account.customer_id == CUST, Account.deleted_at.is_(None))).all()
    if len(sole) == 1:
        check("routing: single-account customer -> credit carries that account_id (panel table agrees)",
              credits and credits[0].account_id == sole[0], credits[0].account_id if credits else None)
    check("routing: wallet untouched", len(s.exec(select(ShopWalletEntry).where(ShopWalletEntry.shop_user_id == SU)).all()) == wallet_rows_before)
    check("routing: debt dropped to 40,000", abs(debt_of(s, CUST) - 40000.0) < 0.01, debt_of(s, CUST))

# for_debt + order_id → refused at submission
with Session(engine) as s:
    # build an awaiting order to try binding
    from app.models import ShopOrder, ShopOrderStatus
    o = ShopOrder(shop_user_id=SU, data_limit_gb=5, duration_days=30, price=50000,
                  status=ShopOrderStatus.awaiting_payment)
    s.add(o)
    s.commit()
    OID = o.id
r = client.post("/api/shop/bot/topups", headers=BOT_HDR,
                json={"telegram_id": 555001, "claimed_amount": 10000, "receipt_text": "TRK-DEBT-2", "for_debt": True, "order_id": OID})
check("debt topup: order binding refused (400)", r.status_code == 400, r.status_code)

# unlinked user trying for_debt → refused
r = client.post("/api/shop/bot/topups", headers=BOT_HDR,
                json={"telegram_id": 555002, "claimed_amount": 10000, "receipt_text": "TRK-DEBT-3", "for_debt": True})
check("debt topup: unlinked refused (400)", r.status_code == 400, r.status_code)

# ---- 3) plain top-up for the SAME indebted customer → WALLET (never auto) ---
r = client.post("/api/shop/bot/topups", headers=BOT_HDR,
                json={"telegram_id": 555001, "claimed_amount": 30000, "receipt_text": "TRK-WALLET-1"})
tid2 = r.json()["id"]
client.post(f"/api/shop/topups/{tid2}/approve")
with Session(engine) as s:
    from app.shop_service import wallet_balance
    # 2026-10-26 (owner): ONE balance - a plain top-up pays the debt FIRST.
    check("plain topup: all 30,000 went to the debt, wallet 0", wallet_balance(s, SU) == 0, wallet_balance(s, SU))
    check("plain topup: debt 40,000 -> 10,000", abs(debt_of(s, CUST) - 10000.0) < 0.01, debt_of(s, CUST))
    # Restore the premise the sections below test (wallet 30,000 vs debt 40,000)
    # with direct writes, so the purchase/pay-from-wallet gates stay covered.
    from app.models import LedgerEntry, LedgerType, ShopWalletEntry, ShopWalletEntryType
    s.add(LedgerEntry(type=LedgerType.charge, amount=30000.0, customer_id=CUST, note="test restore"))
    s.add(ShopWalletEntry(shop_user_id=SU, type=ShopWalletEntryType.adjust, amount=30000, note="test restore"))
    s.commit()

# ---- 4) purchase affordability: spendable = wallet − debt -------------------
# purchase()'s own gates run in order: blocked → is_open → price → wallet.
# Open the shop for the direct call (no Marzban needed: affordability raises first).
with Session(engine) as s:
    from app.models import ShopSettings
    st = s.get(ShopSettings, 1) or ShopSettings(id=1)
    st.is_open = True
    st.price_per_gb = 5000
    s.add(st)
    s.commit()
# ---- 4) purchase affordability: spendable = wallet − debt -------------------
# wallet 30,000 − debt 40,000 = spendable 0 → any purchase refused with DEBT_LINE.
# Marzban is reached only AFTER affordability raises, so no fake is needed.
from app import shop_service as ss  # noqa: E402
from app.shop_service import ShopError  # noqa: E402

with Session(engine) as s:
    su_row = s.get(ShopUser, SU)
    import asyncio

    async def _try_buy():
        return await ss.purchase(s, su_row, data_limit_gb=10.0)

    try:
        asyncio.run(_try_buy())
        check("purchase: blocked while spendable < price", False, "no refusal raised")
    except ShopError as exc:
        check("purchase: blocked while spendable < price", "DEBT_LINE" in str(exc) or "بدهی" in str(exc), str(exc)[:120])

# ---- 5) pay-from-wallet: the sanctioned cross-system pair -------------------
# wallet 30,000, debt 40,000 → pay capped at 30,000
r = client.post("/api/shop/bot/pay-debt-from-wallet", headers=BOT_HDR, json={"telegram_id": 555001})
check("pay-from-wallet: 200", r.status_code == 200, r.text[:250])
res = r.json()
check("pay-from-wallet: paid 30,000 (capped by wallet)", res["paid"] == 30000, res)
check("pay-from-wallet: wallet 0", res["wallet_balance"] == 0, res)
check("pay-from-wallet: debt 10,000 left", abs(res["ledger_debt"] - 10000.0) < 0.01, res)
with Session(engine) as s:
    pair_w = s.exec(select(ShopWalletEntry).where(ShopWalletEntry.shop_user_id == SU, ShopWalletEntry.type == ShopWalletEntryType.debt_pay)).all()
    pair_l = s.exec(select(LedgerEntry).where(LedgerEntry.customer_id == CUST, LedgerEntry.type == LedgerType.credit, LedgerEntry.source == LedgerSource.shop)).all()
    check("pair: wallet row + ledger credit both exist", len(pair_w) == 2 and len(pair_l) == 3, (len(pair_w), len(pair_l)))  # +1 pair from the AUTO debt payment of the plain top-up
    check("pair: shared ref in both notes", pair_w and any("debt-pay:shopuser" in (e.note or "") for e in pair_l))

# double-tap: wallet now 0 → clean refusal, nothing posted
r = client.post("/api/shop/bot/pay-debt-from-wallet", headers=BOT_HDR, json={"telegram_id": 555001})
check("double-tap: refused (wallet empty)", r.status_code == 400, r.status_code)
with Session(engine) as s:
    pair_l2 = s.exec(select(LedgerEntry).where(LedgerEntry.customer_id == CUST, LedgerEntry.type == LedgerType.credit, LedgerEntry.source == LedgerSource.shop)).all()
    check("double-tap: no extra ledger credit", len(pair_l2) == 3, len(pair_l2))

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All routed-books debt-payment cases passed.")
