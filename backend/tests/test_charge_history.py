"""GET /api/history/charges — read-only charge history, 9 test groups:

 1. summary values equal INDEPENDENT raw SQL over the same window
 2. gb_amount=NULL is never flattened to zero (known-only sum + count)
 3. row-less account still listed with an empty summary; deleted account
    (deleted_at / deleted_from_marzban) keeps its history
 4. window boundaries: end-of-day `until`, next-day 00:00 excluded, aware
    (+03:30) since/until normalized to naive UTC
 5. include_credits=false hides credits from `entries` but NOT from summaries
 6. errors: empty/non-numeric ids 400, unknown ids 404 with the list, >50 400,
    since>until 400, missing param 422
 7. packages only status='activated'; markers only the allowed actions;
    detail text passes through un-parsed
 8. strictly read-only: table row counts + ledger sum identical across many calls
 9. live anchor (env VPN_HISTORY_DB=<copy>): endpoint output equals raw SQL on
    the migrated copy for the four Seyed accounts, found by NAME — run as
    `python tests/test_charge_history.py --anchor-copy <path/to/copy.db>`

Plain `python tests/test_charge_history.py` from `backend/` — no pytest, same
as the rest of this suite. Baseline: the pre-existing suite stays green.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

if "--anchor-copy" in sys.argv:
    # Group 9 runs in this same file but against a COPY of the live DB:
    # copy first (the original is never touched), then let init_db() below
    # migrate the copy. WAL sidecars are carried over if present so no
    # checkpointed-but-unmerged transactions are lost.
    _src = Path(sys.argv[sys.argv.index("--anchor-copy") + 1])
    _work = Path(tempfile.mkdtemp(prefix="hist_anchor_")) / "live_copy.db"
    import shutil

    shutil.copy2(_src, _work)
    for _side in ("-wal", "-shm"):
        if Path(str(_src) + _side).exists():
            shutil.copy2(str(_src) + _side, str(_work) + _side)
    os.environ["DATABASE_URL"] = f"sqlite:///{_work.as_posix()}"
else:
    _TMP_DB = Path(tempfile.mkdtemp(prefix="hist_test_")) / "test.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"

os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, AccountEvent, Customer, Group, LedgerEntry, QueuedPlan  # noqa: E402

init_db()
app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def call(ids: list[int], since: str = "2026-07-01", until: str = "2026-08-31", **params):
    p = {"account_ids": ",".join(str(i) for i in ids), "since": since, "until": until, **params}
    return client.get("/api/history/charges", params=p)


def raw_agg(account_id: int, since: str, until: str) -> dict:
    """INDEPENDENT aggregate — raw SQL straight on the tables, no endpoint or
    app-model code involved. Window bounds mirror the CONTRACT (date-only
    until = through the end of that day), not the endpoint's code path."""
    s_dt = f"{since} 00:00:00.000000"
    u_dt = (datetime.fromisoformat(until) + timedelta(days=1) - timedelta(microseconds=1)).strftime("%Y-%m-%d %H:%M:%S.%f")
    with engine.connect() as conn:
        q = text(
            "SELECT type, COUNT(*) n, SUM(amount) total, SUM(gb_amount) gb, COUNT(gb_amount) gb_n, "
            "MIN(date) first_c, MAX(date) last_c FROM ledgerentry "
            "WHERE account_id = :aid AND date >= :s AND date <= :u GROUP BY type"
        )
        out: dict = {}
        for row in conn.execute(q, {"aid": account_id, "s": s_dt, "u": u_dt}):
            out[row.type] = {"n": row.n, "total": row.total, "gb": row.gb, "gb_n": row.gb_n,
                             "first": str(row.first_c), "last": str(row.last_c)}
        return out


def seed() -> dict[int]:
    with Session(engine) as s:
        c1 = Customer(name="Hist Cust")
        s.add(c1)
        s.flush()
        g1 = Group(name="Hist Group", representative_customer_id=c1.id)
        s.add(g1)
        s.flush()

        a1 = Account(marzban_username="hist_payg", customer_id=c1.id, billing_mode="payg", status="active")
        a2 = Account(marzban_username="hist_grouped", customer_id=c1.id, group_id=g1.id, billing_mode="prepay", status="active")
        a3 = Account(marzban_username="hist_empty", customer_id=c1.id, status="active")
        a4 = Account(
            marzban_username="hist_deleted",
            customer_id=c1.id,
            status="deleted_from_marzban",
            deleted_at=datetime(2026, 9, 1, 0, 0, 0),
        )
        s.add_all([a1, a2, a3, a4])
        s.flush()

        def L(acc, type_, amount, when, gb=None, source="web", note=None):
            return LedgerEntry(
                account_id=acc.id, customer_id=acc.customer_id, group_id=acc.group_id,
                type=type_, amount=amount, date=when, gb_amount=gb, source=source, note=note,
            )

        rows = [
            L(a1, "charge", 115000, datetime(2026, 7, 28, 12, 0, 0), gb=45.0, note="45GB package"),
            L(a1, "charge", 50000, datetime(2026, 8, 10, 9, 30, 0), gb=None, note="manual, GB unknown"),
            L(a1, "credit", 300000, datetime(2026, 8, 20, 10, 0, 0)),
            # A2 — includes the boundary pair (23:59:59 in-window, 00:00 next day out)
            L(a2, "charge", 5000, datetime(2026, 7, 31, 21, 0, 0), gb=None),
            L(a2, "charge", 30000, datetime(2026, 8, 1, 8, 0, 0), gb=20.0),
            L(a2, "charge", 62500, datetime(2026, 8, 5, 10, 0, 0), gb=25.0),
            L(a2, "charge", 110250, datetime(2026, 8, 15, 23, 59, 59), gb=None),
            L(a2, "charge", 100000, datetime(2026, 8, 16, 0, 0, 0), gb=50.0),
            L(a2, "credit", 200000, datetime(2026, 8, 10, 12, 0, 0)),
            L(a2, "credit", 101750, datetime(2026, 8, 12, 12, 0, 0)),
            L(a4, "charge", 250000, datetime(2026, 8, 10, 14, 0, 0), gb=5.0, source="sync"),
        ]
        s.add_all(rows)

        s.add_all([
            QueuedPlan(account_id=a2.id, data_limit_gb=20, duration_days=30, status="activated",
                       created_at=datetime(2026, 7, 25), activated_at=datetime(2026, 8, 1, 7, 0, 0)),
            QueuedPlan(account_id=a2.id, data_limit_gb=50, duration_days=30, status="activated",
                       created_at=datetime(2026, 8, 10), activated_at=datetime(2026, 8, 15, 7, 0, 0)),
            QueuedPlan(account_id=a2.id, data_limit_gb=30, duration_days=30, status="cancelled"),
            QueuedPlan(account_id=a2.id, data_limit_gb=10, duration_days=30, status="pending"),
            QueuedPlan(account_id=a1.id, data_limit_gb=5, duration_days=30, status="cancelled"),
        ])

        s.add_all([
            AccountEvent(account_id=a1.id, action="external_data_limit_increase",
                         detail="+45.00 GB added outside this dashboard", date=datetime(2026, 7, 20, 11, 0, 0)),
            AccountEvent(account_id=a1.id, action="adjust", detail="manual +10 GB", date=datetime(2026, 8, 2, 9, 0, 0)),
            # NOT a marker action — must never appear in `markers`
            AccountEvent(account_id=a1.id, action="extend_expire", detail="+30 days", date=datetime(2026, 8, 3, 9, 0, 0)),
            AccountEvent(account_id=a4.id, action="deleted_from_marzban", detail="removed in Marzban",
                         date=datetime(2026, 9, 1, 0, 0, 0)),
        ])
        s.commit()
        return {"a1": a1.id, "a2": a2.id, "a3": a3.id, "a4": a4.id}


def run_main_tests(IDS: dict) -> None:
    A1, A2, A3, A4 = IDS["a1"], IDS["a2"], IDS["a3"], IDS["a4"]

    # ── Group 1: summary == independent raw SQL ────────────────────────────
    resp = call([A1, A2, A4])
    check("200 on the seeded selection", resp.status_code == 200, str(resp.text[:300]))
    body = resp.json()
    for label, aid in (("A1", A1), ("A2", A2), ("A4", A4)):
        sql = raw_agg(aid, "2026-07-01", "2026-08-31")
        summ = body["summaries"][str(aid)]
        ch, cr = sql.get("charge", {}), sql.get("credit", {})
        check(f"{label}: charge_count == SQL", summ["charge_count"] == ch.get("n", 0),
              f"{summ['charge_count']} vs {ch.get('n', 0)}")
        check(f"{label}: charged_amount == SQL", abs(summ["charged_amount"] - (ch.get("total") or 0.0)) < 0.005,
              f"{summ['charged_amount']} vs {ch.get('total')}")
        check(f"{label}: credited_amount == SQL", (summ["credited_amount"] or 0.0) == (cr.get("total") or 0.0),
              f"{summ['credited_amount']} vs {cr.get('total')}")
        check(f"{label}: credit_count == SQL", summ["credit_count"] == cr.get("n", 0))
        known_sql = round(ch["gb"], 3) if ch.get("gb_n", 0) else None
        check(f"{label}: charged_gb_known == SQL (None stays None)",
              summ["charged_gb_known"] == known_sql, f"{summ['charged_gb_known']} vs {known_sql}")
        check(f"{label}: charged_gb_known_count == SQL", summ["charged_gb_known_count"] == ch.get("gb_n", 0))
        if ch:
            first_ok = (summ["first_charge_at"] or "").replace("T", " ").startswith(ch["first"][:19])
            last_ok = (summ["last_charge_at"] or "").replace("T", " ").startswith(ch["last"][:19])
            check(f"{label}: first/last charge == SQL", first_ok and last_ok,
                  f"{summ['first_charge_at']}/{summ['last_charge_at']} vs {ch['first']}/{ch['last']}")
        else:
            check(f"{label}: no charges → first/last None", summ["first_charge_at"] is None)

    first_date = body["entries"][0]["date"]
    check("entry dates serialized with explicit UTC (Z)", first_date.endswith("Z"), first_date)

    # ── Group 2: NULL gb never zero ────────────────────────────────────────
    s1 = body["summaries"][str(A1)]
    check("A1: 2 charges but only 1 with known GB", s1["charge_count"] == 2 and s1["charged_gb_known_count"] == 1)
    check("A1: known-GB sum is 45.0 — NULL row neither counted nor merged", s1["charged_gb_known"] == 45.0)
    s2 = body["summaries"][str(A2)]
    check("A2: 5 charges, 3 known-GB rows (20+25+50=95)",
          s2["charge_count"] == 5 and s2["charged_gb_known"] == 95.0 and s2["charged_gb_known_count"] == 3)
    t = body["totals"]
    check("totals: known GB sums only known rows across selection",
          t["charged_gb_known"] == 145.0 and t["charged_gb_known_count"] == 5,
          f"{t['charged_gb_known']} / {t['charged_gb_known_count']}")
    check("totals: charge_count spans all selected accounts", t["charge_count"] == 2 + 5 + 1)

    # ── Group 3: empty account listed; deleted keeps history ───────────────
    resp = call([A1, A2, A3, A4])
    b = resp.json()
    check("all four requested accounts returned",
          len(b["accounts"]) == 4 and {a["id"] for a in b["accounts"]} == {A1, A2, A3, A4})
    a3 = next(a for a in b["accounts"] if a["id"] == A3)
    check("row-less account present with its username", a3["username"] == "hist_empty")
    s3 = b["summaries"][str(A3)]
    check("row-less summary is zeroed honestly", s3["charge_count"] == 0 and s3["charged_amount"] == 0
          and s3["first_charge_at"] is None and s3["charged_gb_known"] is None)
    a4j = next(a for a in b["accounts"] if a["id"] == A4)
    check("deleted account flagged deleted=True, status preserved",
          a4j["deleted"] is True and a4j["status"] == "deleted_from_marzban")
    s4 = b["summaries"][str(A4)]
    check("deleted account's ledger history still served", s4["charge_count"] == 1 and s4["charged_amount"] == 250000)

    # ── Group 3b: picker listing includes soft-deleted accounts ────────────
    r = client.get("/api/history/accounts")
    check("history/accounts: 200", r.status_code == 200, str(r.status_code))
    la = {a["username"]: a for a in r.json()}
    check("history/accounts: lists ALL four incl. deleted",
          {"hist_payg", "hist_grouped", "hist_empty", "hist_deleted"} <= set(la), str(set(la)))
    check("history/accounts: deleted flagged + status preserved",
          la["hist_deleted"]["deleted"] is True and la["hist_deleted"]["status"] == "deleted_from_marzban")
    check("history/accounts: alive accounts flagged false",
          la["hist_payg"]["deleted"] is False and la["hist_empty"]["deleted"] is False)
    check("history/accounts: owner names attached",
          la["hist_grouped"]["group_name"] == "Hist Group" and la["hist_payg"]["customer_name"] == "Hist Cust")

    # ── Group 4: window boundaries ─────────────────────────────────────────
    b = call([A2], since="2026-08-01", until="2026-08-15").json()
    amts = sorted(e["amount"] for e in b["entries"] if e["type"] == "charge")
    check("date-only until: 23:59:59 row INSIDE", 110250 in amts, str(amts))
    check("date-only until: next-day 00:00 row OUTSIDE", 100000 not in amts, str(amts))
    check("date-only until: rows after since inside (07-31 row is before since)",
          30000 in amts and 62500 in amts and 5000 not in amts, str(amts))

    b = call([A2], since="2026-08-01T00:00:00+03:30", until="2026-08-20").json()
    amts = [e["amount"] for e in b["entries"] if e["type"] == "charge"]
    check("aware since +03:30 (=2026-07-31T20:30Z): 21:00Z row INSIDE", 5000 in amts, str(amts))
    b = call([A2], since="2026-08-01", until="2026-08-16T00:00:00+03:30").json()
    amts = [e["amount"] for e in b["entries"] if e["type"] == "charge"]
    check("aware until +03:30 (=2026-08-15T20:30Z): 23:59:59Z row OUTSIDE", 110250 not in amts, str(amts))
    check("aware until +03:30: same-day earlier rows INSIDE", 62500 in amts and 30000 in amts, str(amts))

    # ── Group 5: include_credits ───────────────────────────────────────────
    b = call([A1, A2], include_credits="false").json()
    check("include_credits=false: no credit rows in entries",
          all(e["type"] == "charge" for e in b["entries"]))
    check("include_credits=false: A1 summary credits NOT zeroed",
          b["summaries"][str(A1)]["credit_count"] == 1
          and b["summaries"][str(A1)]["credited_amount"] == 300000.0)
    b = call([A1, A2], include_credits="true").json()
    check("include_credits=true: credits present in entries", "credit" in {e["type"] for e in b["entries"]})

    # ── Group 6: errors ────────────────────────────────────────────────────
    r = client.get("/api/history/charges", params={"account_ids": ""})
    check("empty account_ids → 400", r.status_code == 400, str(r.status_code))
    r = client.get("/api/history/charges", params={"account_ids": "abc"})
    check("non-numeric account_ids → 400", r.status_code == 400, str(r.status_code))
    r = client.get("/api/history/charges", params={"account_ids": f"{A1},3.5"})
    check("float token → 400", r.status_code == 400, str(r.status_code))
    r = call([999999])
    check("unknown id → 404 with the id named", r.status_code == 404 and "999999" in r.json()["detail"], r.text[:200])
    r = call([999998, 999999])
    check("multiple unknown ids → 404 listing BOTH",
          r.status_code == 404 and "999998" in r.json()["detail"] and "999999" in r.json()["detail"])
    r = call(list(range(900, 900 + 51)))
    check("51 accounts → 400", r.status_code == 400, str(r.status_code))
    r = call([A1], since="2026-08-20", until="2026-08-01")
    check("since>until → 400", r.status_code == 400, str(r.status_code))
    r = client.get("/api/history/charges", params={"since": "2026-08-01"})
    check("missing account_ids → 422 (required param)", r.status_code == 422, str(r.status_code))

    # ── Group 6b: council-found bugs (empty token, default-since anchor) ──
    r = client.get("/api/history/charges", params={"account_ids": f"{A1},,{A2}"})
    check("empty token between commas ('3,,46') -> 400, not silently dropped", r.status_code == 400, str(r.status_code))
    r = client.get("/api/history/charges", params={"account_ids": f"{A1},"})
    check("trailing comma -> 400", r.status_code == 400, str(r.status_code))
    # only `until`, far enough in the past that "now - 180d" would land AFTER it —
    # the old code anchored the default `since` to `now`, so this always 400'd.
    r = client.get("/api/history/charges", params={"account_ids": str(A1), "until": "2025-01-05"})
    check("only an old `until` given: default `since` anchors to `until`, not `now` (no 400)",
          r.status_code == 200, r.text[:200])

    # ── Group 7: packages + markers ────────────────────────────────────────
    # until extended past the A4 deletion event (2026-09-01) so the marker
    # is inside the window; markers must also RESPECT the window.
    b = call([A1, A2, A4], until="2026-09-30").json()
    pkgs = [(p["account_id"], p["data_limit_gb"]) for p in b["packages"]]
    check("packages: only the two activated plans of A2 (20 then 50 GB)",
          pkgs == [(A2, 20.0), (A2, 50.0)], str(pkgs))
    check("packages: activated_at present", all(p["activated_at"] for p in b["packages"]))
    # A narrower window that EXCLUDES both A2 packages' activation dates: the
    # unfiltered query used to return them anyway (found by multi-model review).
    b_narrow = call([A2], since="2026-08-20", until="2026-08-20").json()
    check("packages respect the window: a window with neither activation date returns none",
          b_narrow["packages"] == [], b_narrow["packages"])
    acts = [(m["account_id"], m["action"]) for m in b["markers"]]
    allowed = {"external_data_limit_increase", "adjust", "external_usage_reset",
               "payg_cap_hit_reset", "settle_reset", "deleted_from_marzban"}
    check("markers: allowed actions only", {a for _, a in acts} <= allowed, str(acts))
    check("markers: extend_expire never included", all(a != "extend_expire" for _, a in acts))
    m45 = next(m for m in b["markers"] if m["action"] == "external_data_limit_increase")
    check("markers: detail passes through UNCHANGED (never parsed)",
          m45["detail"] == "+45.00 GB added outside this dashboard", m45["detail"])
    check("markers: deleted_from_marzban present for A4 (window extended)", (A4, "deleted_from_marzban") in acts, str(acts))
    b = call([A1, A2, A4]).json()  # default window ends 2026-08-31
    check("markers respect the window: deletion event (09-01) outside until=08-31",
          all(m["action"] != "deleted_from_marzban" for m in b["markers"]))

    # ── Group 8: strictly read-only ────────────────────────────────────────
    def snapshot() -> dict:
        with engine.connect() as conn:
            tables = [r[0] for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))]
            snap = {t: conn.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar() for t in tables}
            snap["__ledger_sum"] = conn.execute(text("SELECT COALESCE(SUM(amount),0) FROM ledgerentry")).scalar()
            return snap

    before = snapshot()
    for _ in range(10):
        call([A1, A2, A3, A4])
        call([A2], include_credits="true")
        call([999999])  # error paths must not write either
        client.get("/api/history/charges", params={"account_ids": "junk"})
    after = snapshot()
    check("read-only: every table's row count + ledger sum unchanged after ~40 calls",
          before == after,
          str({k: (before.get(k), after.get(k)) for k in set(before) | set(after) if before.get(k) != after.get(k)}))


# §2 of the task brief — sanity reference (soft): the copy is the source of
# truth, so endpoint-vs-SQL mismatches FAIL but drift from these values only WARNs.
ANCHOR_EXPECTED = {
    "Seyed": {"charges": 1, "total": 115000.0, "credits": 0},
    "Seyed_brother2": {"charges": 4, "total": 302750.0, "credits": 2},
    "Seyed_brother": {"charges": 1, "total": 250000.0, "credits": 0},
    "real_seyed": {"charges": 2, "total": 666134.62, "credits": 1},
}


def run_anchor() -> None:
    """Endpoint vs raw SQL on the migrated live copy, accounts found BY NAME."""
    names = list(ANCHOR_EXPECTED)
    with Session(engine) as s:
        accs = {a.marzban_username: a.id for a in s.exec(select(Account)).all()}
    found = {n: accs[n] for n in names if n in accs}
    check("anchor: all four Seyed accounts found by name", len(found) == 4, str(list(accs)))
    if len(found) != 4:
        return
    ids = list(found.values())
    # Live rows span 2026-07-15..2026-09-20 — the window covers all of it.
    resp = call(ids, since="2026-07-01", until="2026-09-30")
    check("anchor: 200 on the live copy", resp.status_code == 200, str(resp.text[:300]))
    if resp.status_code != 200:
        return
    b = resp.json()
    for name, aid in found.items():
        sql = raw_agg(aid, "2026-07-01", "2026-09-30")
        summ = b["summaries"][str(aid)]
        ch, cr = sql.get("charge", {}), sql.get("credit", {})
        check(f"anchor/{name}: charge_count == raw SQL", summ["charge_count"] == ch.get("n", 0),
              f"{summ['charge_count']} vs {ch.get('n', 0)}")
        check(f"anchor/{name}: charged_amount == raw SQL",
              abs(summ["charged_amount"] - (ch.get("total") or 0.0)) < 0.005,
              f"{summ['charged_amount']} vs {ch.get('total')}")
        check(f"anchor/{name}: credit_count == raw SQL", summ["credit_count"] == cr.get("n", 0))
        exp = ANCHOR_EXPECTED[name]
        sane = summ["charge_count"] == exp["charges"] and abs(summ["charged_amount"] - exp["total"]) < 0.01 \
            and summ["credit_count"] == exp["credits"]
        level = "OK" if sane else "WARN"
        print(f"[{level}] anchor/{name}: §2 sanity — charges={summ['charge_count']} "
              f"total={summ['charged_amount']} credits={summ['credit_count']} (expected {exp})")
        if not sane:
            print("        (copy may have moved past the 23:48 snapshot — SQL comparison above is the binding one)")


if "--anchor-copy" in sys.argv:
    run_anchor()
else:
    IDS = seed()
    run_main_tests(IDS)
    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        sys.exit(1)
    print("All charge-history endpoint cases passed.")
if failures and "--anchor-copy" in sys.argv:
    print(f"{len(failures)} ANCHOR FAILURES: {failures}")
    sys.exit(1)
sys.exit(0)
