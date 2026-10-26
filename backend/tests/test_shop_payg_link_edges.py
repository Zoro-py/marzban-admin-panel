"""Edge cases of the one-click settle+convert+link and the @username lookup
(2026-10-25). Each case asserts BOTH the HTTP outcome AND ledger/billing_mode
state - a 409 that already converted money is a failure here.

Plain `python -m tests.test_shop_payg_link_edges` from `backend/`.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="paygedge_test_")) / "test.db"
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

def new_customer(name, accounts, **kw):
    with Session(engine) as s:
        c = Customer(name=name, **kw)
        s.add(c)
        s.flush()
        ids = []
        for (uname, mode, used_gb, rate, extra) in accounts:
            a = Account(marzban_username=uname, customer_id=c.id, billing_mode=mode,
                        used_traffic=int(used_gb * GB), usage_baseline=0, rate_per_gb=rate, **extra)
            s.add(a)
            s.flush()
            ids.append(a.id)
        s.commit()
        return c.id, ids


def charges_for(acc_ids):
    with Session(engine) as s:
        return round(sum(e.amount for e in s.exec(
            select(LedgerEntry).where(LedgerEntry.account_id.in_(acc_ids), LedgerEntry.type == LedgerType.charge)).all()), 2)


def mode_of(acc_id):
    with Session(engine) as s:
        return s.get(Account, acc_id).billing_mode


def bind(cid, tg, convert=True):
    return client.post(f"/api/shop/link/{cid}/telegram", json={"telegram_id": tg, "convert_payg": convert})


from app.models import ShopUser, ShopBindClaim  # noqa: E402

P, Q_ = BillingMode.payg, BillingMode.prepay

# E1 - ONE Telegram may carry several customers (owner 2026-10-26: one person
# buys for the whole family). Binding a payg customer to an id that already
# carries another customer converts + links normally.
other, _ = new_customer("Already Linked", [("el_prepay", Q_, 0, 1000.0, {})])
assert bind(other, 710001, convert=False).status_code == 200
victim, (v1,) = new_customer("Victim Payg", [("e1_payg", P, 10, 10000.0, {})])
r = bind(victim, 710001)
check("E1: id already on another customer -> still 200 (multi-link)", r.status_code == 200, r.text[:200])
check("E1: ...converted + charged once (100,000)", abs(charges_for([v1]) - 100000.0) < 0.01 and mode_of(v1) == Q_, (charges_for([v1]), mode_of(v1)))
check("E1: ...card names the sibling customer", "Already Linked" in client.get(f"/api/shop/link/{victim}").json()["also_linked_customers"])

# E2 - customer already linked to a DIFFERENT telegram: same rule.
c2, (a2,) = new_customer("Linked Elsewhere Payg", [("e2_payg", P, 5, 10000.0, {})])
with Session(engine) as s:
    su = ShopUser(telegram_id=710020)
    s.add(su)
    s.flush()
    cu = s.get(Customer, c2)
    cu.shop_user_id = su.id
    s.add(cu)
    s.commit()
r = bind(c2, 710021)
check("E2: customer linked elsewhere -> 409", r.status_code == 409, r.text[:200])
check("E2: ...nothing charged, still payg", charges_for([a2]) == 0.0 and mode_of(a2) == P)

# E3 - group representative: refused before any conversion.
rep, (g1,) = new_customer("Group Rep", [("e3_payg", P, 5, 10000.0, {})], is_group_rep=True)
r = bind(rep, 710030)
check("E3: group rep -> 409", r.status_code == 409, r.text[:120])
check("E3: ...nothing charged", charges_for([g1]) == 0.0 and mode_of(g1) == P)

# E4 - mixed customer: prepay + payg standalone. Only the payg one is billed/flipped.
mx, (m_pre, m_pay) = new_customer("Mixed", [("e4_pre", Q_, 7, 9999.0, {}), ("e4_pay", P, 4, 10000.0, {})])
r = bind(mx, 710040)
check("E4: mixed convert 200", r.status_code == 200, r.text[:200])
check("E4: only the payg account billed (40,000)", charges_for([m_pre]) == 0.0 and abs(charges_for([m_pay]) - 40000.0) < 0.01, (charges_for([m_pre]), charges_for([m_pay])))
check("E4: both prepay now", mode_of(m_pre) == Q_ and mode_of(m_pay) == Q_)

# E5 - double click / replay: second convert is idempotent, no second charge.
dc, (d1,) = new_customer("Double Click", [("e5_payg", P, 3, 10000.0, {})])
r1 = bind(dc, 710050)
r2 = bind(dc, 710050)
check("E5: first 200", r1.status_code == 200, r1.text[:150])
check("E5: replay 200, converts nothing more", r2.status_code == 200 and r2.json().get("converted_payg_accounts") == 0, r2.text[:150])
check("E5: charged exactly once (30,000)", abs(charges_for([d1]) - 30000.0) < 0.01, charges_for([d1]))

# E6 - partial failure: 2nd account's Marzban reset fails -> error, first stays
# converted (real, charged), retry resumes and never double-charges the first.
pf, (p1, p2) = new_customer("Partial", [("e6_a", P, 2, 10000.0, {}), ("e6_b", P, 3, 10000.0, {})])
state = {"fail": True}


async def flaky(username):
    if username == "e6_b" and state["fail"]:
        raise RuntimeError("marzban down")
    return {"username": username, "used_traffic": 0, "status": "active"}


accounts_module.marzban_client.reset_user = flaky
try:
    r = bind(pf, 710060)
    code = r.status_code
    txt = r.text[:150]
except Exception as exc:  # TestClient re-raises unhandled server errors
    code, txt = 500, repr(exc)
check("E6: failed attempt is NOT a 2xx", code >= 400, (code, txt))
check("E6: first converted+charged (20,000), second untouched", abs(charges_for([p1]) - 20000.0) < 0.01 and mode_of(p1) == Q_ and mode_of(p2) == P and charges_for([p2]) == 0.0, (charges_for([p1]), mode_of(p1), mode_of(p2)))
state["fail"] = False
r = bind(pf, 710060)
check("E6: retry 200 resumes", r.status_code == 200, r.text[:150])
check("E6: retry: p1 NOT re-charged, p2 charged once (30,000)", abs(charges_for([p1]) - 20000.0) < 0.01 and abs(charges_for([p2]) - 30000.0) < 0.01, (charges_for([p1]), charges_for([p2])))
accounts_module.marzban_client.reset_user = _fake_reset_user

# E7 - zero-usage payg account converts with 0 charge.
z, (z1,) = new_customer("Zero Use", [("e7_payg", P, 0, 10000.0, {})])
r = bind(z, 710070)
check("E7: zero usage converts, settled 0", r.status_code == 200 and r.json().get("settled_amount") == 0, r.text[:150])
check("E7: now prepay", mode_of(z1) == Q_)

# E8 - deleted-from-Marzban / disabled payg accounts must not wedge the link.
dd, (x1, x2) = new_customer("Dead Accounts", [("e8_del", P, 2, 10000.0, {"status": "deleted_from_marzban"}), ("e8_dis", P, 1, 10000.0, {"status": "disabled"})])
try:
    r = bind(dd, 710080)
    print("   E8 detail:", r.status_code, r.text[:200], "charged:", charges_for([x1, x2]), "modes:", mode_of(x1), mode_of(x2))
    check("E8: deleted+disabled payg: link completes (200)", r.status_code == 200, r.text[:200])
except Exception as exc:
    check("E8: deleted+disabled payg: link completes (200)", False, repr(exc))

# ── claim ARRIVAL for payg customers (council 2026-10-25) ─────────────────
# @username claims never bind or convert a payg customer by themselves: the
# person's first bot message only RECORDS their numeric id; the operator then
# confirms settle+convert+link in the panel.
from app.routers.shop import _try_complete_bind_claim  # noqa: E402
from app.shop_service import get_or_create_shop_user  # noqa: E402


def say(tg_id, username=None, name=None):
    """What the bot's /session call does: upsert the ShopUser, run the hook."""
    with Session(engine) as s:
        su = get_or_create_shop_user(s, tg_id, telegram_username=username, display_name=name)
        _try_complete_bind_claim(s, su)


def claim(cid, **kw):
    return client.post("/api/shop/bind-claim", json={"customer_id": cid, **kw})


def state(cid):
    return client.get(f"/api/shop/link/{cid}").json()


az, (az1,) = new_customer("Azno", [("azno_payg", P, 8, 12000.0, {})])
r = claim(az, telegram_username="@TajikMH")
check("C1: payg customer can hold a claim", r.status_code == 200, r.text[:160])
check("C1: state: pending, nobody arrived yet", state(az)["bind_claim_username"] == "tajikmh" and state(az)["bind_claim_arrived_telegram_id"] is None, state(az))

say(720001, username="someone_else", name="Not Azno")
check("C2: unrelated sender -> no arrival", state(az)["bind_claim_arrived_telegram_id"] is None)

say(720002, username="tajikmh", name="Tajik M")
st = state(az)
check("C3: arrival recorded with the sender's numeric id + name", st["bind_claim_arrived_telegram_id"] == 720002 and st["bind_claim_arrived_name"] == "Tajik M", st)
check("C3: NOT linked, still payg, nothing charged, claim still pending", st["status"] == "not_linked" and mode_of(az1) == P and charges_for([az1]) == 0.0 and st["bind_claim_username"] == "tajikmh", st)

say(720002, username="tajikmh", name="Tajik M")
check("C4: same person writes again -> idempotent", state(az)["bind_claim_arrived_telegram_id"] == 720002 and mode_of(az1) == P and charges_for([az1]) == 0.0)

# username changed hands: the LATEST sender shows; the operator's confirm
# carries an explicit id, so a swap between view and click cannot redirect it.
say(720003, username="tajikmh", name="Different Person")
check("C5: new holder of the username replaces the arrival (operator sees the name)", state(az)["bind_claim_arrived_telegram_id"] == 720003 and state(az)["bind_claim_arrived_name"] == "Different Person")

# operator confirms the id he actually saw (720002): converts + links THAT id.
r = bind(az, 720002)
check("C6: operator confirm with the id he saw -> 200", r.status_code == 200 and r.json().get("converted_payg_accounts") == 1, r.text[:200])
check("C6: charged 96,000 (8 GB x 12,000), now prepay, linked to 720002", abs(charges_for([az1]) - 96000.0) < 0.01 and mode_of(az1) == Q_ and state(az)["linked_telegram_id"] == 720002, state(az))
with Session(engine) as s:
    left = s.exec(select(ShopBindClaim).where(ShopBindClaim.customer_id == az, ShopBindClaim.claimed_at.is_(None))).all()
check("C6: pending claim cleared by the bind", left == [])

# two customers claiming the SAME username: nothing arrives for either.
d1c, (d1a,) = new_customer("Dup One", [("dup1", P, 1, 1000.0, {})])
d2c, (d2a,) = new_customer("Dup Two", [("dup2", P, 1, 1000.0, {})])
claim(d1c, telegram_username="dupuser")
claim(d2c, telegram_username="dupuser")
say(720010, username="dupuser", name="Dup")
check("C7: ambiguous (two claims match) -> no arrival on either", state(d1c)["bind_claim_arrived_telegram_id"] is None and state(d2c)["bind_claim_arrived_telegram_id"] is None)

# display-name claim arrives the same way.
nm, (nm1,) = new_customer("By Name", [("byname", P, 1, 1000.0, {})])
claim(nm, display_name="Ali Rezaei")
say(720020, username=None, name="ali  REZAEI")
check("C8: display-name claim arrival (normalised match)", state(nm)["bind_claim_arrived_telegram_id"] == 720020 and mode_of(nm1) == P)

# GROUP payg: claim refused up front (flip the group first).
with Session(engine) as s:
    gc = Customer(name="In Payg Group")
    s.add(gc)
    s.flush()
    g = Group(name="Edge Grp", billing_mode=BillingMode.payg, representative_customer_id=gc.id)
    s.add(g)
    s.flush()
    s.add(Account(marzban_username="grp_member", customer_id=gc.id, group_id=g.id, billing_mode=P, rate_per_gb=1000.0))
    s.commit()
    GC = gc.id
r = claim(GC, telegram_username="grpuser")
check("C9: group-payg customer: claim refused (409)", r.status_code == 409, r.text[:160])

# PREPAY customers keep the old behaviour: the first message binds at once.
pp, (pp1,) = new_customer("Plain Prepay", [("pp_acc", Q_, 1, 1000.0, {})])
claim(pp, telegram_username="plainuser")
say(720030, username="plainuser", name="Plain")
check("C10: prepay customer still auto-binds on first message", state(pp)["status"] == "linked" and state(pp)["linked_telegram_id"] == 720030, state(pp))

# arrival whose id is ALREADY on another customer: allowed (multi-link) - converts once.
tk, (tk1,) = new_customer("Taken Id", [("tk", P, 5, 1000.0, {})])
claim(tk, telegram_username="takenuser")
say(720030, username="takenuser", name="Plain")   # 720030 already carries Plain Prepay
check("C11: arrival recorded", state(tk)["bind_claim_arrived_telegram_id"] == 720030)
r = bind(tk, 720030)
check("C11: confirm -> 200, billed 5,000, linked next to the sibling", r.status_code == 200 and abs(charges_for([tk1]) - 5000.0) < 0.01 and "Plain Prepay" in state(tk)["also_linked_customers"], r.text[:160])

# E12 - a service the operator deleted from the panel must vanish from the bot
# list (live 2026-10-26: «shop10» kept showing as 30 GB / 28 days left).
from app.models import ShopOrder, ShopOrderStatus  # noqa: E402

BOT_HEADERS = {"X-Shop-Bot-Key": "test-shop-key"}
with Session(engine) as s:
    su = ShopUser(telegram_id=730001)
    s.add(su)
    s.flush()
    live = Account(marzban_username="e12_live", billing_mode=Q_, used_traffic=0, data_limit=30 * GB)
    dead = Account(marzban_username="e12_dead", billing_mode=Q_, used_traffic=0, data_limit=30 * GB, status="deleted_from_marzban")
    s.add(live)
    s.add(dead)
    s.flush()
    for acc, n in ((live, 1), (dead, 1), (dead, 2)):  # the dead one has TWO orders (top-up)
        s.add(ShopOrder(shop_user_id=su.id, data_limit_gb=30, duration_days=30, price=1000,
                        status=ShopOrderStatus.delivered, account_id=acc.id, marzban_username=acc.marzban_username))
    s.commit()
names = [r["marzban_username"] for r in client.get("/api/shop/bot/accounts", params={"telegram_id": 730001}, headers=BOT_HEADERS).json()]
check("E12: live shop service listed", names == ["e12_live"], names)
check("E12: deleted-from-panel service (even with 2 orders) not listed", "e12_dead" not in names, names)

# ── M: one Telegram, several customers (family) ───────────────────────────
from app.models import ShopWalletEntry, ShopWalletEntryType, LedgerEntry as _LE  # noqa: E402
from app.services import MoneyBook as _MB  # noqa: E402


def owe(cid):
    with Session(engine) as s:
        return round(_MB(s).customer_net(s.get(Customer, cid)), 2)


def add_charge(cid, amount):
    with Session(engine) as s:
        s.add(LedgerEntry(type=LedgerType.charge, amount=amount, customer_id=cid, note="m-test"))
        s.commit()


def fund_wallet(tg, amount):
    with Session(engine) as s:
        su = s.exec(select(ShopUser).where(ShopUser.telegram_id == tg)).one()
        s.add(ShopWalletEntry(shop_user_id=su.id, type=ShopWalletEntryType.topup, amount=amount, note="m-test"))
        s.commit()


TG = 740001
fa, (fa1,) = new_customer("Fam A", [("fam_a1", Q_, 1, 1000.0, {})])
fb, (fb1, fb2) = new_customer("Fam B", [("fam_b1", Q_, 1, 1000.0, {}), ("fam_b2", Q_, 1, 1000.0, {})])
assert bind(fa, TG, convert=False).status_code == 200
assert bind(fb, TG, convert=False).status_code == 200
rows = client.get("/api/shop/bot/accounts", params={"telegram_id": TG}, headers=BOT_HEADERS).json()
names = sorted(r["marzban_username"] for r in rows)
check("M1: bot lists the accounts of BOTH linked customers", names == ["fam_a1", "fam_b1", "fam_b2"], names)
check("M2: each customer's card shows the sibling", state(fa)["also_linked_customers"] == ["Fam B"] and state(fb)["also_linked_customers"] == ["Fam A"], (state(fa)["also_linked_customers"], state(fb)["also_linked_customers"]))
check("M3: card shows id, not 'not linked'", state(fa)["status"] == "linked" and state(fa)["linked_telegram_id"] == TG)

add_charge(fa, 30000.0)
add_charge(fb, 50000.0)
sess = client.post("/api/shop/bot/session", headers=BOT_HEADERS, json={"telegram_id": TG}).json()
check("M4b: session carries the per-customer split", sorted((r["name"], r["debt"]) for r in sess.get("debt_breakdown", [])) == [("Fam A", 30000.0), ("Fam B", 50000.0)], sess.get("debt_breakdown"))
check("M4: session debt = SUM of both customers (80,000)", abs(sess.get("ledger_debt", -1) - 80000.0) < 0.01, sess.get("ledger_debt"))

fund_wallet(TG, 100000)
r = client.post("/api/shop/bot/pay-debt-from-wallet", headers=BOT_HEADERS, json={"telegram_id": TG, "amount": 60000})
check("M5: wallet pays 60,000 across the family", r.status_code == 200 and r.json()["paid"] == 60000, r.text[:200])
check("M5: oldest customer first, each capped at own debt (A 30,000 -> 0, B 50,000 -> 20,000)", owe(fa) == 0.0 and abs(owe(fb) - 20000.0) < 0.01, (owe(fa), owe(fb)))
with Session(engine) as s:
    su = s.exec(select(ShopUser).where(ShopUser.telegram_id == TG)).one()
    from app.shop_service import wallet_balance as _wb
    check("M5: wallet debited exactly 60,000 (one entry)", _wb(s, su.id) == 40000, _wb(s, su.id))
r = client.post("/api/shop/bot/pay-debt-from-wallet", headers=BOT_HEADERS, json={"telegram_id": TG, "amount": 999999})
check("M6: asking for more than owed pays only the remaining 20,000", r.status_code == 200 and r.json()["paid"] == 20000 and abs(owe(fb)) < 0.01, r.text[:200])
r = client.post("/api/shop/bot/pay-debt-from-wallet", headers=BOT_HEADERS, json={"telegram_id": TG})
check("M7: nothing owed -> refused, no money moves", r.status_code == 400, r.text[:120])
with Session(engine) as s:
    su = s.exec(select(ShopUser).where(ShopUser.telegram_id == TG)).one()
    check("M7: wallet untouched by the refusal (20,000 left)", _wb(s, su.id) == 20000, _wb(s, su.id))

# a credit on one customer never offsets another's debt
add_charge(fa, 10000.0)
with Session(engine) as s:
    s.add(LedgerEntry(type=LedgerType.credit, amount=500000.0, customer_id=fb, note="m-test prepaid"))
    s.commit()
sess = client.post("/api/shop/bot/session", headers=BOT_HEADERS, json={"telegram_id": TG}).json()
check("M8: B's big credit does NOT hide A's 10,000 debt", abs(sess.get("ledger_debt", -1) - 10000.0) < 0.01, sess.get("ledger_debt"))

# nickname visibility covers every linked customer's accounts
with Session(engine) as s:
    fb_acc = s.exec(select(Account).where(Account.marzban_username == "fam_b2")).one().id
r = client.post(f"/api/shop/bot/accounts/{fb_acc}/name", headers=BOT_HEADERS, json={"telegram_id": TG, "name": "Sister"})
check("M9: can rename a second customer's account", r.status_code == 200, r.text[:150])
# a stranger's account stays invisible
r = client.post(f"/api/shop/bot/accounts/{fb_acc}/name", headers=BOT_HEADERS, json={"telegram_id": 730001, "name": "Mine"})
check("M10: ...but not from an unrelated telegram", r.status_code == 404, r.text[:120])

# unlink one customer: the other stays linked, list shrinks
assert client.delete(f"/api/shop/link/{fb}").status_code == 200
rows = client.get("/api/shop/bot/accounts", params={"telegram_id": TG}, headers=BOT_HEADERS).json()
check("M11: unlinking B leaves A linked and listed alone", [r["marzban_username"] for r in rows] == ["fam_a1"] and state(fa)["status"] == "linked", rows)

# M12 - a RECEIPT designated as debt payment is split the same way (oldest first,
# capped per customer); the part exceeding the TOTAL debt is credited to the
# first customer - nothing is dropped. Wallet untouched.
import asyncio  # noqa: E402
from app.shop_service import create_topup, approve_topup  # noqa: E402

rb, (rb1,) = new_customer("Rcpt B", [("rcpt_b", Q_, 0, 1000.0, {})])
ra, (ra1,) = new_customer("Rcpt A", [("rcpt_a", Q_, 0, 1000.0, {})])
TG2 = 740002
assert bind(ra, TG2, convert=False).status_code == 200
assert bind(rb, TG2, convert=False).status_code == 200
add_charge(ra, 40000.0)
add_charge(rb, 10000.0)
with Session(engine) as s:
    su = s.exec(select(ShopUser).where(ShopUser.telegram_id == TG2)).one()
    t = create_topup(s, su, 70000, None, for_debt=True)
    t, _ = asyncio.run(approve_topup(s, t, approved_amount=70000))
check("M12: receipt 70,000 vs debts 40,000 + 10,000 -> both cleared, 20,000 surplus credited to the first (lowest customer id = Rcpt B)", owe(ra) == 0.0 and owe(rb) == -20000.0, (owe(ra), owe(rb)))
with Session(engine) as s:
    su = s.exec(select(ShopUser).where(ShopUser.telegram_id == TG2)).one()
    check("M12: wallet untouched by a debt-designated receipt", _wb(s, su.id) == 0, _wb(s, su.id))

# M13 - the panel card carries the customer's WHOLE debt, not just the unbilled usage
# (owner 2026-10-26: Azno showed 142k while owing 949k).
ow, (ow1,) = new_customer("Owes Much", [("owes_much", P, 5, 10000.0, {})])
add_charge(ow, 800000.0)
st = client.get(f"/api/shop/link/{ow}").json()
check("M13: owed_now = posted 800,000 + pending 50,000; pending alone is 50,000", abs(st["owed_now"] - 850000.0) < 0.01 and abs(st["payg_pending_amount"] - 50000.0) < 0.01, st)

# E11 - the amount the panel shows before the click equals what is charged.
pv, (pv1,) = new_customer("Preview Match", [("e11_payg", P, 6, 12345.0, {})])
shown = client.get(f"/api/shop/link/{pv}").json()["payg_pending_amount"]
bind(pv, 710110)
check("E11: preview == settled", abs(shown - charges_for([pv1])) < 0.01, (shown, charges_for([pv1])))

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All payg-link edge cases passed.")
