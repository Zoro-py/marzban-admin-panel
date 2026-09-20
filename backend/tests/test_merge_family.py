"""scripts/merge_family_customers.py — folds one-account customers into one
family customer without moving a single Toman.

Plain `python tests/test_merge_family.py` from `backend/`. Builds a throwaway
DB with the app's real schema, so a column rename in models.py breaks this
test instead of the operator's live run.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="merge_test_"))
_DB = _TMP / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.db import init_db  # noqa: E402

_spec = importlib.util.spec_from_file_location("merge_family", BACKEND.parent / "scripts" / "merge_family_customers.py")
mf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mf)

_failures: list[str] = []


def check(label, actual, expected) -> None:
    if actual == expected:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}\n          expected: {expected!r}\n          actual:   {actual!r}")
        _failures.append(label)


def _seed() -> None:
    """khanevadeh1..3 + Khanevadeh4: plain personal customers (4 has a credit
    and a customer-only row). Khanevadeh5 represents a group, khanevadeh6's
    account is a group member — both must be skipped. Solo is unrelated."""
    c = sqlite3.connect(_DB)
    c.execute("DELETE FROM ledgerentry"); c.execute("DELETE FROM account"); c.execute('DELETE FROM "group"'); c.execute("DELETE FROM customer")
    ts = "2026-09-14 10:00:00.000000"
    for cid, name in [(1, "khanevadeh1"), (2, "khanevadeh2"), (3, "khanevadeh3"), (4, "Khanevadeh4"),
                      (5, "Khanevadeh5"), (6, "khanevadeh6"), (7, "Solo")]:
        c.execute("INSERT INTO customer (id, name, is_group_rep, kind, created_at) VALUES (?,?,0,'individual',?)", (cid, name, ts))
    c.execute('INSERT INTO "group" (id, name, representative_customer_id, billing_mode, billing_cycle_days, created_at) '
              "VALUES (1,'G',5,'payg',30,?)", (ts,))
    for aid, user, cust, grp in [(1, "khanevadeh1", 1, None), (2, "khanevadeh2", 2, None), (3, "khanevadeh3", 3, None),
                                 (4, "Khanevadeh4", 4, None), (6, "khanevadeh6", 6, 1), (7, "solo", 7, None)]:
        c.execute("INSERT INTO account (id, marzban_username, customer_id, group_id, role, billing_mode, used_traffic, lifetime_used_traffic, "
                  "usage_baseline, billed_data_limit, first_seen_traffic, auto_renew_enabled, created_at) "
                  "VALUES (?,?,?,?,'primary','prepay',0,0,0,0,0,1,?)", (aid, user, cust, grp, ts))
    rows = [  # (type, amount, customer, account, group)
        ("charge", 50000, 1, 1, None), ("charge", 50000, 3, 3, None), ("charge", 75000, 4, 4, None),
        ("credit", 25000, 4, 4, None), ("charge", 10000, 4, None, None),  # customer-only row on #4
        ("charge", 99999, 6, 6, 1), ("charge", 12345, 7, 7, None),
    ]
    for t, amt, cust, acc, grp in rows:
        c.execute("INSERT INTO ledgerentry (type, amount, date, customer_id, account_id, group_id, source) "
                  "VALUES (?,?,?,?,?,?,'web')", (t, amt, ts, cust, acc, grp))
    c.commit(); c.close()


def _q(sql, *a):
    c = sqlite3.connect(_DB)
    try:
        return c.execute(sql, a).fetchall()
    finally:
        c.close()


def test_dry_run_changes_nothing() -> None:
    print("\n[1] dry-run: full report, invariants hold, database untouched")
    _seed()
    before = (_q("SELECT id,name FROM customer ORDER BY id"), _q("SELECT id,customer_id FROM account ORDER BY id"),
              _q("SELECT id,customer_id FROM ledgerentry ORDER BY id"))
    conn = sqlite3.connect(_DB, isolation_level=None)
    rep = mf.merge(conn, "khanevadeh", "khanevadeh", apply=False)
    conn.close()
    after = (_q("SELECT id,name FROM customer ORDER BY id"), _q("SELECT id,customer_id FROM account ORDER BY id"),
             _q("SELECT id,customer_id FROM ledgerentry ORDER BY id"))
    check("nothing changed", after, before)
    check("not committed", rep["committed"], False)
    check("merges the 4 plain ones", sorted(m["id"] for m in rep["merged"]), [1, 2, 3, 4])
    check("skips group rep + group member", sorted(s["id"] for s in rep["skipped"]), [5, 6])
    inv = rep["invariants"]
    check("ledger total unchanged", inv["ledger_total_before"], inv["ledger_total_after"])
    check("family posted == sum of sources (50k+0+50k+ (75k-25k+10k))", inv["family_posted_after"], 160000.0)


def test_apply_merges_and_conserves_money() -> None:
    print("\n[2] --apply: accounts and ledger follow the family; not a Toman moves")
    _seed()
    total0 = _q("SELECT SUM(CASE WHEN type='charge' THEN amount ELSE -amount END) FROM ledgerentry")[0][0]
    conn = sqlite3.connect(_DB, isolation_level=None)
    rep = mf.merge(conn, "khanevadeh", "khanevadeh", apply=True)
    conn.close()
    check("committed", rep["committed"], True)
    tid = rep["target"]["id"]
    check("target is new + family", (rep["target"]["created"], _q("SELECT kind FROM customer WHERE id=?", tid)[0][0]), (True, "family"))
    check("sources deleted", _q("SELECT id FROM customer WHERE id IN (1,2,3,4)"), [])
    check("skipped customers untouched", sorted(r[0] for r in _q("SELECT id FROM customer WHERE id IN (5,6,7)")), [5, 6, 7])
    check("four accounts now owned by the family", sorted(r[0] for r in _q("SELECT id FROM account WHERE customer_id=?", tid)), [1, 2, 3, 4])
    check("group-member account untouched", _q("SELECT customer_id FROM account WHERE id=6")[0][0], 6)
    total1 = _q("SELECT SUM(CASE WHEN type='charge' THEN amount ELSE -amount END) FROM ledgerentry")[0][0]
    check("ledger total identical", total1, total0)
    fam = _q("SELECT SUM(CASE WHEN type='charge' THEN amount ELSE -amount END) FROM ledgerentry "
             "WHERE account_id IN (SELECT id FROM account WHERE customer_id=?) OR (account_id IS NULL AND group_id IS NULL AND customer_id=?)",
             tid, tid)[0][0]
    check("family posted", fam, 160000.0)
    check("no ledger row left on a deleted customer", _q("SELECT COUNT(*) FROM ledgerentry WHERE customer_id IN (1,2,3,4)")[0][0], 0)


def test_extends_existing_family_case_insensitive() -> None:
    print("\n[3] an existing same-named customer (different case) is reused, not duplicated")
    _seed()
    c = sqlite3.connect(_DB)
    c.execute("INSERT INTO customer (id, name, is_group_rep, kind, created_at) VALUES (20,'KHANEVADEH',0,'individual','2026-09-14 10:00:00.000000')")
    c.execute("INSERT INTO account (id, marzban_username, customer_id, role, billing_mode, used_traffic, lifetime_used_traffic, usage_baseline, "
              "billed_data_limit, first_seen_traffic, auto_renew_enabled, created_at) "
              "VALUES (30,'other',20,'primary','prepay',0,0,0,0,0,1,'2026-09-14 10:00:00.000000')")
    c.commit(); c.close()
    conn = sqlite3.connect(_DB, isolation_level=None)
    rep = mf.merge(conn, "khanevadeh", "khanevadeh", apply=True)
    conn.close()
    check("reused id 20", (rep["target"]["id"], rep["target"]["created"]), (20, False))
    check("now family", _q("SELECT kind FROM customer WHERE id=20")[0][0], "family")
    check("its own account kept + 4 merged", len(_q("SELECT id FROM account WHERE customer_id=20")), 5)


def test_refuses_when_nothing_to_do() -> None:
    print("\n[4] nothing eligible → clear refusal, no writes")
    _seed()
    conn = sqlite3.connect(_DB, isolation_level=None)
    try:
        mf.merge(conn, "nomatchbase", "x", apply=True)
        raised = False
    except SystemExit:
        raised = True
    finally:
        conn.close()
    check("SystemExit raised", raised, True)
    check("db intact", len(_q("SELECT id FROM customer")), 7)


def test_refuses_delegate_target_and_lone_target() -> None:
    print(chr(10) + "[5] target linked to a delegate is refused; a lone eligible customer that IS the target has nothing to merge")
    _seed()
    c = sqlite3.connect(_DB)
    c.execute("INSERT INTO customer (id, name, is_group_rep, kind, created_at) VALUES (21,'khanevadeh',0,'individual','2026-09-14 10:00:00.000000')")
    c.execute("INSERT INTO delegate (id, customer_id, telegram_id, is_active, daily_create_cap, username_prefix, default_duration_days, created_at) "
              "VALUES (1,21,555,1,20,'d',30,'2026-09-14 10:00:00.000000')")
    c.commit(); c.close()
    conn = sqlite3.connect(_DB, isolation_level=None)
    try:
        mf.merge(conn, "khanevadeh", "khanevadeh", apply=True)
        msg = ""
    except SystemExit as e:
        msg = str(e)
    finally:
        conn.close()
    check("delegate-linked target refused", "delegate" in msg, True)
    check("nothing moved", _q("SELECT customer_id FROM account WHERE id=1")[0][0], 1)
    probe = sqlite3.connect(_DB, timeout=1, isolation_level=None)
    try:
        probe.execute("BEGIN IMMEDIATE")   # would raise "database is locked" if the failed merge left its transaction open
        probe.execute("ROLLBACK")
        writer_ok = True
    except sqlite3.OperationalError:
        writer_ok = False
    finally:
        probe.close()
    check("no transaction left open after a refusal (a new writer can start)", writer_ok, True)

    _seed()
    c = sqlite3.connect(_DB)
    c.execute("DELETE FROM ledgerentry WHERE account_id IN (2,3,4) OR customer_id IN (2,3,4)")
    c.execute("DELETE FROM account WHERE id IN (2,3,4)"); c.execute("DELETE FROM customer WHERE id IN (2,3,4)")
    c.commit(); c.close()
    conn = sqlite3.connect(_DB, isolation_level=None)
    try:
        mf.merge(conn, "khanevadeh", "khanevadeh1", apply=True)
        msg2 = ""
    except SystemExit as e:
        msg2 = str(e)
    finally:
        conn.close()
    check("target == the only eligible customer -> nothing to merge", "nothing to merge" in msg2, True)


def main() -> int:
    init_db()
    test_dry_run_changes_nothing()
    test_apply_merges_and_conserves_money()
    test_extends_existing_family_case_insensitive()
    test_refuses_when_nothing_to_do()
    test_refuses_delegate_target_and_lone_target()
    print()
    if _failures:
        print(f"RESULT: {len(_failures)} FAILED: {_failures}")
        return 1
    print("RESULT: all checks OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
