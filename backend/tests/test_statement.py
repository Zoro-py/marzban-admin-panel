"""GET /api/ledger/statement — the bank-statement data behind "Balance since".

Plain `python -m tests.test_statement` from `backend/` (same harness shape as
tests/test_balance_since.py). Marzban usage is faked; the money side runs on a
real temp SQLite DB through the real endpoint.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="statement_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
# A fake Marzban DB (users + user_usage_logs), created in section 4.
_MZ_DB = Path(tempfile.mkdtemp(prefix="statement_mz_")) / "db.sqlite3"
os.environ["MARZBAN_DB_PATH"] = str(_MZ_DB)
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
for _k in ("BOT_TOKEN", "BOT_ADMIN_CHAT_ID", "SHOP_BOT_TOKEN", "SHOP_BOT_API_KEY", "DELEGATE_BOT_API_KEY"):
    os.environ[_k] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, func, select  # noqa: E402

from app import marzban_client as marzban_module  # noqa: E402
from app import statement as statement_module  # noqa: E402
from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.marzban_client import MarzbanUnavailable  # noqa: E402
from app.models import (  # noqa: E402
    Account, AccountEvent, AppSettings, BillingMode, Customer, Group, LedgerEntry, LedgerType,
    QueuedPlan, QueuedPlanStatus,
)
from app.services import MoneyBook  # noqa: E402

init_db()
failures: list[str] = []


def check(label: str, cond: bool, detail: object = "") -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        failures.append(label)


GB = 1024 ** 3

# --- fake Marzban usage: 1 GB per started day in [start, end) per user, unless
# the user is in FAIL (raises Unavailable) --------------------------------------
FAIL: set[str] = set()
CALLS: list[tuple[str, str, str]] = []


async def fake_usage(username: str, start: str, end: str):
    CALLS.append((username, start, end))
    if username in FAIL:
        raise MarzbanUnavailable("down")
    s, e = datetime.fromisoformat(start), datetime.fromisoformat(end)
    return int((e - s).total_seconds() / 86400 * GB)


marzban_module.marzban_client.get_user_usage_bytes = fake_usage
app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)


def stmt(**params):
    statement_module._usage_cache.clear()
    r = client.get("/api/ledger/statement", params=params)
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


now = datetime.now(timezone.utc).replace(tzinfo=None)
with Session(engine) as s:
    s.add(AppSettings(id=1, default_rate_per_gb=5000))
    cust = Customer(name="Stmt Cust")
    rep = Customer(name="Stmt Rep")
    s.add_all([cust, rep])
    s.commit()
    s.refresh(cust)
    s.refresh(rep)
    grp = Group(name="Stmt Grp", representative_customer_id=rep.id, billing_mode=BillingMode.prepay)
    s.add(grp)
    s.commit()
    s.refresh(grp)
    pre = Account(marzban_username="st_pre", customer_id=cust.id, billing_mode=BillingMode.prepay,
                  data_limit=50 * GB, billed_data_limit=0, used_traffic=3 * GB)
    payg = Account(marzban_username="st_payg", customer_id=cust.id, billing_mode=BillingMode.payg,
                   data_limit=300 * GB, billed_data_limit=300 * GB, used_traffic=4 * GB, usage_baseline=0)
    member = Account(marzban_username="st_member", customer_id=rep.id, group_id=grp.id, billing_mode=BillingMode.prepay,
                     data_limit=20 * GB, billed_data_limit=20 * GB, used_traffic=1 * GB)
    s.add_all([pre, payg, member])
    s.commit()
    for a in (pre, payg, member):
        s.refresh(a)
    ids = {"cust": cust.id, "rep": rep.id, "grp": grp.id, "pre": pre.id, "payg": payg.id, "member": member.id}

    old = now - timedelta(days=40)
    mid = now - timedelta(days=10)
    # The «Mahan» carry-over shape on st_pre: an old charge, settled by a payment INSIDE the window.
    s.add(LedgerEntry(type=LedgerType.charge, amount=300_000, account_id=pre.id, customer_id=cust.id, date=old, note="old package"))
    s.add(LedgerEntry(type=LedgerType.credit, amount=300_000, account_id=pre.id, customer_id=cust.id, date=mid))
    s.add(LedgerEntry(type=LedgerType.charge, amount=50_000, account_id=pre.id, customer_id=cust.id, date=mid + timedelta(days=1),
                      gb_amount=10.0, note="+10GB"))
    s.add(LedgerEntry(type=LedgerType.charge, amount=20_000, account_id=payg.id, customer_id=cust.id, date=mid + timedelta(days=2)))
    s.add(LedgerEntry(type=LedgerType.charge, amount=7_000, customer_id=cust.id, date=mid + timedelta(days=3), note="customer-only fee"))
    s.add(LedgerEntry(type=LedgerType.charge, amount=40_000, account_id=member.id, group_id=grp.id, customer_id=rep.id, date=mid))
    s.add(LedgerEntry(type=LedgerType.charge, amount=3_000, group_id=grp.id, customer_id=rep.id, date=mid, note="group-only"))
    # Recharges on st_pre within the window
    s.add(QueuedPlan(account_id=pre.id, data_limit_gb=30.0, duration_days=31, status=QueuedPlanStatus.activated, activated_at=mid))
    s.add(QueuedPlan(account_id=pre.id, data_limit_gb=99.0, duration_days=31, status=QueuedPlanStatus.cancelled, activated_at=mid))
    s.add(QueuedPlan(account_id=pre.id, data_limit_gb=40.0, duration_days=31, status=QueuedPlanStatus.activated, activated_at=old))
    s.add(AccountEvent(account_id=pre.id, action="adjust", detail="extend_gb=10.0, extend_days=5 | note=+10 cash", date=mid + timedelta(days=1)))
    s.add(AccountEvent(account_id=pre.id, action="adjust", detail="extend_gb=-2.0", date=mid + timedelta(days=1)))
    s.add(AccountEvent(account_id=pre.id, action="adjust", detail="set_data_limit_gb=80.0", date=mid + timedelta(days=1)))
    s.add(AccountEvent(account_id=pre.id, action="delegate_renew", detail="extend_gb=5, extend_days=30, delegate_id=1", date=mid))
    s.add(AccountEvent(account_id=pre.id, action="external_data_limit_increase", detail="+15.00 GB added outside this dashboard (in Marzban directly)", date=mid))
    s.add(AccountEvent(account_id=payg.id, action="external_data_limit_increase", detail="+285.00 GB added outside this dashboard (in Marzban directly)", date=mid))
    s.add(AccountEvent(account_id=pre.id, action="adjust", detail="garbage extend_gb=999", date=mid))
    s.commit()
    ledger_before = s.exec(select(func.count()).select_from(LedgerEntry)).one()

since = (now - timedelta(days=20)).replace(tzinfo=timezone.utc).isoformat()

# ---- 1) the statement equation, and parity with MoneyBook («Owes now») -------------
with Session(engine) as s:
    book = MoneyBook(s)
    expected = {
        "account_id": (ids["pre"], book.account_posted(s.get(Account, ids["pre"])), book.account_net(s.get(Account, ids["pre"]))),
        "group_id": (ids["grp"], book.group_posted(s.get(Group, ids["grp"])), book.group_net(s.get(Group, ids["grp"]))),
        "customer_id": (ids["cust"], book.customer_posted(s.get(Customer, ids["cust"])), book.customer_net(s.get(Customer, ids["cust"]))),
    }
for kind, (sid, posted, net) in expected.items():
    st = stmt(**{kind: sid, "since": since, "usage": "false"})["statement"]
    check(f"{kind}: opening + charged - paid == closing",
          abs(st["opening_balance"] + st["charged"] - st["paid"] - st["closing_balance"]) < 0.01, st)
    check(f"{kind}: closing == MoneyBook posted (all-time, open window)", abs(st["closing_balance"] - posted) < 0.01, (st["closing_balance"], posted))
    check(f"{kind}: owed_now == MoneyBook net («Owes now»)", abs(st["owed_now"] - net) < 0.01, (st["owed_now"], net))

# ---- 2) the carry-over case reads correctly --------------------------------------
st = stmt(account_id=ids["pre"], since=since, usage="false")
m = st["statement"]
check("carry-over: opening balance is the old 300,000 charge", m["opening_balance"] == 300_000)
check("carry-over: 1 charge 50,000 and 1 payment 300,000 in the window", (m["charge_count"], m["charged"], m["payment_count"], m["paid"]) == (1, 50_000, 1, 300_000))
check("carry-over: closing posted 50,000 (not a misleading -250,000 credit)", m["closing_balance"] == 50_000)
check("carry-over: owed_now positive = closing + pending package (50GB unbilled × 5000)", m["owed_now"] == 50_000 + 250_000, m["owed_now"])
check("charges_gb: GB known on 1 of 1 charge", st["charges_gb"] == {"gb": 10.0, "with_gb": 1, "of": 1}, st["charges_gb"])
bal = [e["balance_after"] for e in st["entries"]]
check("entries: running balance starts from the opening and ends at closing", bal == [0, 50_000], bal)

# ---- 3) recharges -----------------------------------------------------------
v = st["volume"]
check("recharges: activated package in window counted (30GB), cancelled/old excluded", v["by_kind"]["package"] == {"gb": 30.0, "count": 1}, v["by_kind"])
check("recharges: panel adjust parsed through its ' | note=' suffix (10GB)", v["by_kind"]["panel"] == {"gb": 10.0, "count": 1}, v["by_kind"])
check("recharges: delegate renewal counted (5GB)", v["by_kind"]["delegate"] == {"gb": 5.0, "count": 1})
check("recharges: change made in Marzban counted for a PREPAY account (15GB)", v["by_kind"]["outside_panel"] == {"gb": 15.0, "count": 1})
check("recharges: total 60GB over 4 recharges", (v["recharged_gb"], v["recharge_count"]) == (60.0, 4), (v["recharged_gb"], v["recharge_count"]))
check("recharges: the -2GB reduction is reported as excluded, not subtracted silently",
      any(r["gb"] == -2.0 for r in v["excluded"]), v["excluded"])
st_payg = stmt(account_id=ids["payg"], since=since, usage="false")
check("recharges: +285GB cap move on a PAYG account is excluded (standard-shape sweep, not a sale)",
      st_payg["volume"]["recharged_gb"] == 0 and any(r["gb"] == 285.0 for r in st_payg["volume"]["excluded"]), st_payg["volume"])
check("payg_equivalent: a PAYG account's own row is always None (nothing hypothetical about what it already is)",
      st_payg["accounts"][0]["payg_equivalent"] is None, st_payg["accounts"])
check("prepay_vs_payg: a pure-PAYG scope has no prepay account to compare -> None",
      st_payg["prepay_vs_payg"] is None, st_payg["prepay_vs_payg"])
check("recharges: malformed detail and set_data_limit are never parsed into GB",
      not any(r["gb"] in (999.0, 80.0) for r in st["recharges"] + v["excluded"]))

# ---- 4) usage from Marzban's reset log --------------------------------------------
import sqlite3  # noqa: E402

r = stmt(account_id=ids["pre"], since=since)
check("usage: Marzban usage log not mounted -> unavailable, money still returned",
      r["volume"]["usage_status"] == "unavailable" and r["volume"]["used_gb"] is None and r["statement"]["charge_count"] == 1, r["volume"])
check("payg_equivalent: a prepay account with unknown usage is None, not a silent 0",
      r["accounts"][0]["payg_equivalent"] is None, r["accounts"])
check("prepay_vs_payg: unknown usage on the only prepay account -> payg_equivalent/diff None, counted as missing",
      r["prepay_vs_payg"]["payg_equivalent"] is None and r["prepay_vs_payg"]["diff"] is None
      and r["prepay_vs_payg"]["accounts_missing_usage"] == 1,
      r["prepay_vs_payg"])

# st_pre lived three periods: created -60d, reset at -30d (40 GB used), reset
# at `mid` (-10d, 25 GB used), and the open period since then (8 GB so far).
# st_payg is NOT in Marzban's DB (deleted there).
created, r1 = now - timedelta(days=60), now - timedelta(days=30)
con = sqlite3.connect(_MZ_DB)
con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, created_at DATETIME, used_traffic BIGINT)")
con.execute("CREATE TABLE user_usage_logs (id INTEGER PRIMARY KEY, user_id INT, used_traffic_at_reset BIGINT, reset_at DATETIME)")
con.execute("INSERT INTO users VALUES (1, 'st_pre', ?, ?)", (str(created), 8 * GB))
con.execute("INSERT INTO user_usage_logs (user_id, used_traffic_at_reset, reset_at) VALUES (1, ?, ?)", (40 * GB, str(r1)))
con.execute("INSERT INTO user_usage_logs (user_id, used_traffic_at_reset, reset_at) VALUES (1, ?, ?)", (25 * GB, str(mid)))
con.commit()
con.close()


def iso(d):
    return d.replace(tzinfo=timezone.utc).isoformat()


after, until = iso(now - timedelta(days=3)), iso(now - timedelta(days=1))  # used by the error cases in section 6


u = stmt(account_id=ids["pre"], since=iso(r1))["volume"]
check("usage: window starting exactly at a reset -> whole periods, exact (25 + 8 = 33 GB)",
      u["usage_status"] == "complete" and abs(u["used_gb"] - 33) < 0.01 and u["used_estimated_gb"] == 0, u)
u = stmt(account_id=ids["pre"], since=iso(now - timedelta(days=20)))["volume"]
check("usage: window cutting an OLD period -> that period prorated and labeled (12.5 est + 8 exact)",
      u["usage_status"] == "estimated" and abs(u["used_gb"] - 20.5) < 0.01 and abs(u["used_estimated_gb"] - 12.5) < 0.01, u)
check("usage: no ratio is computed from an estimated usage figure", u["utilization_pct"] is None and u["toman_per_used_gb"] is None)
CALLS.clear()
u = stmt(account_id=ids["pre"], since=iso(now - timedelta(days=5)))["volume"]
check("usage: window inside the CURRENT period -> Marzban per-node history (exact, 5 days x 1 GB)",
      u["usage_status"] == "complete" and abs(u["used_gb"] - 5) < 0.05, u)
check("usage: the history call never reaches back past the current period's reset",
      CALLS and all(datetime.fromisoformat(c[1]) >= mid - timedelta(seconds=1) for c in CALLS), CALLS)
r = stmt(account_id=ids["pre"], since="2000-01-01T00:00:00Z")
check("usage: all-time = every period exactly (40 + 25 + 8 = 73 GB)",
      r["volume"]["usage_status"] == "complete" and abs(r["volume"]["used_gb"] - 73) < 0.01, r["volume"])
check("usage: month rows add up to the window usage",
      abs(sum(m["used_gb"] or 0 for m in r["months"]) - r["volume"]["used_gb"]) < 0.05, [(m["month"], m["used_gb"]) for m in r["months"]])
per = stmt(account_id=ids["pre"], since=iso(now - timedelta(days=40)))["periods"]
check("periods: three package periods overlap the window", len(per) == 3, per)
check("periods: each shows its exact usage (40 / 25 / 8 GB)", [p["used_gb"] for p in per] == [40, 25, 8], [p["used_gb"] for p in per])
check("periods: the first starts before the window -> flagged partial_in_window", per[0]["partial_in_window"] and not per[1]["partial_in_window"])
check("periods: the open period is matched to its 30 GB package -> 26.7% used",
      per[2]["is_open"] and per[2]["package_gb"] == 30.0 and per[2]["used_pct"] == 26.7, per[2])
# A late manual charge that names the package ("QueuedPlan #<id>") is that period's bill;
# a reference to a different plan whose id merely shares a prefix is not.
with Session(engine) as s:
    plan30 = s.exec(select(QueuedPlan).where(QueuedPlan.account_id == ids["pre"], QueuedPlan.data_limit_gb == 30.0)).first()
    plan30_id = plan30.id
    s.add(LedgerEntry(type=LedgerType.charge, amount=150_000, account_id=ids["pre"], customer_id=ids["cust"],
                      date=now - timedelta(days=2), gb_amount=30.0, note=f"Recovery charge: 30GB package (QueuedPlan #{plan30_id}, ...)"))
    s.add(LedgerEntry(type=LedgerType.charge, amount=9, account_id=ids["pre"], customer_id=ids["cust"],
                      date=now - timedelta(days=2), note=f"unrelated (QueuedPlan #{plan30_id}9)"))
    s.commit()
per = stmt(account_id=ids["pre"], since=iso(now - timedelta(days=40)))["periods"]
check("periods: a late charge naming the package (QueuedPlan #id) is matched as its bill, the #id9 one is not",
      per[2]["billed"] == 150_000 and per[2]["billed_gb"] == 30.0, per[2])
# A SECOND charge matching the same period (e.g. a correction posted after
# the first) must be SUMMED into billed/billed_gb, not have billed_gb take
# only the first match's GB while billed sums both amounts (the two fields
# would then describe different sets of charges).
with Session(engine) as s:
    s.add(LedgerEntry(type=LedgerType.charge, amount=20_000, account_id=ids["pre"], customer_id=ids["cust"],
                      date=now - timedelta(days=1), gb_amount=4.0, note=f"Correction (QueuedPlan #{plan30_id}, ...)"))
    s.commit()
per = stmt(account_id=ids["pre"], since=iso(now - timedelta(days=40)))["periods"]
check("periods: billed_gb SUMS every matching charge's GB, same as billed sums their amounts",
      per[2]["billed"] == 150_000 + 20_000 and per[2]["billed_gb"] == 30.0 + 4.0, per[2])
FAIL.add("st_pre")
u = stmt(account_id=ids["pre"], since=iso(now - timedelta(days=5)))["volume"]
check("usage: per-node history down -> falls back to a prorated estimate, never silent",
      u["usage_status"] == "estimated" and u["used_gb"] is not None and u["used_estimated_gb"] > 0, u)
FAIL.clear()
r = stmt(customer_id=ids["cust"], since=iso(r1))
check("usage: an account unknown to Marzban -> partial, the other still counted",
      r["volume"]["usage_status"] == "partial" and abs((r["volume"]["used_gb"] or 0) - 33) < 0.01
      and any("st_payg" in e for e in r["volume"]["usage_errors"]), r["volume"])

# ---- 5) Shamsi month buckets use Tehran midnight -------------------------------
with Session(engine) as s:
    c2 = Customer(name="Month Edge")
    s.add(c2)
    s.commit()
    s.refresh(c2)
    a2 = Account(marzban_username="st_edge", customer_id=c2.id, billing_mode=BillingMode.prepay, data_limit=10 * GB, billed_data_limit=10 * GB)
    s.add(a2)
    s.commit()
    s.refresh(a2)
    # 1405/07/01 00:10 Tehran = 2026-09-22 20:40 UTC; 1405/06/31 23:50 Tehran = 2026-09-22 20:20 UTC
    s.add(LedgerEntry(type=LedgerType.charge, amount=1_000, account_id=a2.id, customer_id=c2.id, date=datetime(2026, 9, 22, 20, 40)))
    s.add(LedgerEntry(type=LedgerType.charge, amount=2_000, account_id=a2.id, customer_id=c2.id, date=datetime(2026, 9, 22, 20, 20)))
    s.commit()
    edge_id = a2.id
r = stmt(account_id=edge_id, since="2026-09-01T00:00:00Z", until="2026-10-05T00:00:00Z", usage="false")
months = {mo["month"]: mo for mo in r["months"]}
check("months: labels are Shamsi 1405/06 and 1405/07", set(months) == {"1405/06", "1405/07"}, list(months))
check("months: 23:50 Tehran stays in Shahrivar, 00:10 Tehran lands in Mehr",
      months["1405/06"]["charged"] == 2_000 and months["1405/07"]["charged"] == 1_000, {k: v["charged"] for k, v in months.items()})
check("months: month money sums to the window total",
      sum(mo["charged"] for mo in r["months"]) == r["statement"]["charged"])

# ---- 5b) "changed in Marzban" is judged by the mode AT THAT TIME ------------------
with Session(engine) as s:
    c3 = Customer(name="Mode History")
    s.add(c3)
    s.commit()
    s.refresh(c3)
    pg = Group(name="Payg Grp MH", representative_customer_id=c3.id, billing_mode=BillingMode.payg)
    s.add(pg)
    s.commit()
    s.refresh(pg)
    # payg TODAY, but was prepay until a billing_change 5 days ago
    sw = Account(marzban_username="mh_switched", customer_id=c3.id, billing_mode=BillingMode.payg,
                 data_limit=300 * GB, billed_data_limit=300 * GB)
    # standalone-prepay field, but joined a PAYG group 6 days ago
    gj = Account(marzban_username="mh_grouped", customer_id=c3.id, group_id=pg.id, billing_mode=BillingMode.prepay,
                 data_limit=300 * GB, billed_data_limit=300 * GB)
    s.add_all([sw, gj])
    s.commit()
    s.refresh(sw)
    s.refresh(gj)
    t_switch, t_join = now - timedelta(days=5), now - timedelta(days=6)
    s.add(AccountEvent(account_id=sw.id, action="billing_change", detail="rate_per_gb=None, billing_mode=BillingMode.payg, auto_renew_enabled=True", date=t_switch))
    s.add(AccountEvent(account_id=sw.id, action="external_data_limit_increase", detail="+20.00 GB added outside this dashboard (in Marzban directly)", date=t_switch - timedelta(days=3)))
    s.add(AccountEvent(account_id=sw.id, action="external_data_limit_increase", detail="+270.00 GB added outside this dashboard (in Marzban directly)", date=t_switch + timedelta(minutes=1)))
    s.add(AccountEvent(account_id=gj.id, action="relationship_change", detail=f"{{'group_id': {pg.id}}}", date=t_join))
    s.add(AccountEvent(account_id=gj.id, action="external_data_limit_increase", detail="+7.00 GB added outside this dashboard (in Marzban directly)", date=t_join - timedelta(days=2)))
    s.add(AccountEvent(account_id=gj.id, action="external_data_limit_increase", detail="+285.00 GB added outside this dashboard (in Marzban directly)", date=t_join + timedelta(days=1)))
    s.commit()
    sw_id, gj_id = sw.id, gj.id
wide = (now - timedelta(days=30)).replace(tzinfo=timezone.utc).isoformat()
v = stmt(account_id=sw_id, since=wide, usage="false")["volume"]
check("mode history: +20GB while still prepay (before the switch) counts as a recharge", v["recharged_gb"] == 20.0, v["recharged_gb"])
check("mode history: +270GB right after switching to payg is a cap move, excluded",
      [r["gb"] for r in v["excluded"]] == [270.0], v["excluded"])
v = stmt(account_id=gj_id, since=wide, usage="false")["volume"]
check("mode history: +7GB before joining the payg group counts (standalone prepay then)", v["recharged_gb"] == 7.0, v["recharged_gb"])
check("mode history: +285GB after joining the payg group is excluded", [r["gb"] for r in v["excluded"]] == [285.0], v["excluded"])

# ---- 5c) all-time window: months start at the first activity, usage still measured ----
r = stmt(account_id=ids["pre"], since="2000-01-01T00:00:00Z")
check("all-time: month rows start at the first activity, not in 1378", r["months"] and r["months"][0]["month"] >= "1405/01", r["months"][:1])
check("all-time: at most 13 month rows, ending with the current month",
      len(r["months"]) <= 13 and r["months"][-1]["end"][:10] >= (now - timedelta(days=1)).date().isoformat())
check("all-time: usage is every Marzban period exactly (not a stray 0)",
      r["volume"]["usage_status"] == "complete" and abs((r["volume"]["used_gb"] or 0) - 73) < 0.01, r["volume"]["used_gb"])
check("all-time: month money still sums to the window total",
      abs(sum(m["charged"] for m in r["months"]) - r["statement"]["charged"]) < 0.01)
# st_pre has no account-level rate -> falls back to AppSettings.default_rate_per_gb (5000).
# 73 GB used all-time x 5000 = 365,000: what st_pre would have been charged on PAYG.
check("payg_equivalent: prepay account, known usage, default rate -> used_gb x rate",
      abs(r["accounts"][0]["payg_equivalent"] - 73 * 5000) < 1, r["accounts"][0])
# prepay_charged is charged + pending (the full committed package cost,
# settled or not — see the module docstring on why posted-only was a live
# bug), not just r["statement"]["charged"].
expected_prepay_charged = r["statement"]["charged"] + r["statement"]["pending"]
check("prepay_vs_payg: single-prepay-account scope mirrors charged+pending, not charged alone",
      r["prepay_vs_payg"]["prepay_charged"] == expected_prepay_charged
      and abs(r["prepay_vs_payg"]["payg_equivalent"] - 73 * 5000) < 1
      and abs(r["prepay_vs_payg"]["diff"] - (expected_prepay_charged - 73 * 5000)) < 1
      and r["prepay_vs_payg"]["accounts_missing_usage"] == 0,
      r["prepay_vs_payg"])
# Regression (Masih-mom, 2026-10-11 live): an account with a currently OPEN,
# never-yet-settled package has charged=0 (prepay only posts a charge when a
# package ENDS) but real usage already accrued and already counted on the
# PAYG side — pending must be folded in, or this reads as "prepay is
# magically cheaper", which can't be true for equal used_gb.
check("prepay_vs_payg: an open unsettled package's pending package price counts toward prepay_charged, not just its 0 posted charges",
      r["statement"]["pending"] > 0 and r["prepay_vs_payg"]["prepay_charged"] >= r["statement"]["pending"],
      (r["statement"]["pending"], r["prepay_vs_payg"]["prepay_charged"]))
# Regression: effective_rate() NEVER returns None (it falls through to the
# dashboard default, which itself falls back to 0) — a rate of exactly 0 is
# a real, deliberate price (a comp/free account), not "rate unconfigured".
# It must stay IN the sum as 0, never be read as missing data.
with Session(engine) as s:
    acc = s.get(Account, ids["pre"])
    acc.rate_per_gb = 0.0
    s.add(acc)
    s.commit()
r0 = stmt(account_id=ids["pre"], since="2000-01-01T00:00:00Z")
check("payg_equivalent: a deliberate 0 rate (comp account) -> 0, not None/'missing'",
      r0["accounts"][0]["payg_equivalent"] == 0.0 and r0["prepay_vs_payg"]["payg_equivalent"] == 0.0
      and r0["prepay_vs_payg"]["accounts_missing_usage"] == 0,
      r0["accounts"][0])
with Session(engine) as s:
    acc = s.get(Account, ids["pre"])
    acc.rate_per_gb = None
    s.add(acc)
    s.commit()
# cust scope = st_pre (prepay) + st_payg (payg): the payg account must be
# excluded from prepay_charged/payg_equivalent even though both are in scope.
r_cust = stmt(customer_id=ids["cust"], since="2000-01-01T00:00:00Z")
check("prepay_vs_payg: a PAYG sibling in the same scope never leaks into the prepay comparison",
      abs(r_cust["prepay_vs_payg"]["prepay_charged"] - r["prepay_vs_payg"]["prepay_charged"]) < 1
      and abs(r_cust["prepay_vs_payg"]["payg_equivalent"] - r["prepay_vs_payg"]["payg_equivalent"]) < 1,
      (r_cust["prepay_vs_payg"], r["prepay_vs_payg"]))

# ---- 6) errors -------------------------------------------------------------------
check("errors: two scopes -> 400", client.get("/api/ledger/statement", params={"account_id": 1, "customer_id": 1, "since": since}).status_code == 400)
check("errors: no scope -> 400", client.get("/api/ledger/statement", params={"since": since}).status_code == 400)
check("errors: unknown id -> 404", client.get("/api/ledger/statement", params={"account_id": 99999, "since": since}).status_code == 404)
check("errors: since >= until -> 400", client.get("/api/ledger/statement", params={"account_id": ids["pre"], "since": until, "until": after}).status_code == 400)
check("errors: missing since -> 422", client.get("/api/ledger/statement", params={"account_id": ids["pre"]}).status_code == 422)

# ---- 7) read-only -----------------------------------------------------------------
with Session(engine) as s:
    ledger_after = s.exec(select(func.count()).select_from(LedgerEntry)).one() - 5  # minus the 2 month-edge + 2 plan-ref + 1 billed_gb-sum rows added above
check("read-only: no ledger row written by any statement call", ledger_after == ledger_before, (ledger_before, ledger_after))

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All statement cases passed.")
