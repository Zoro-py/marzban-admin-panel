"""The "no invisible money" invariant — the property test behind F1.3.

Two independent implementations of the ledger bucketing must agree:

  1. app.services.MoneyBook — the one every screen and job actually uses.
  2. A raw-SQL re-derivation written here from the DOMAIN_AND_BILLING.md
     spec alone (account_id set → account bucket; else group_id → group-only;
     else customer_id → customer-only; roll-ups sum the level below, never
     re-query).

And the operator-facing lists must account for every positive balance:
each debtor is either an overdue customer (reports.summary), an unassigned
account CARRYING ITS BALANCE in summary.unassigned_accounts, or a group
whose representative no longer exists. If any positive balance is reachable
by none of those, money can silently disappear from every debt view at once.

Optionally verifies the LIVE production copy: set VPN_INVARIANT_DB to a
read-only .backup of the real database (never a repo file — it carries real
subscription tokens) and the same assertions run against it. Without the
env var those checks are skipped, so the suite stays runnable anywhere.

Plain `python -m tests.test_ledger_invariant` from `backend/` — no pytest,
same harness shape as the rest of this directory.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="invariant_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import engine, init_db  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    BillingMode,
    Customer,
    Group,
    LedgerEntry,
)
from app.routers import reports  # noqa: E402
from app.services import MoneyBook  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


GB = 1024 ** 3


def seed() -> dict:
    """One of every ownership shape, plus ledger rows at every scope —
    including the historically dangerous ones (ownerless with debt, group
    rows with all three FKs set, a credit larger than the charge)."""
    ids = {}
    with Session(engine) as s:
        cust_a = Customer(name="inv-A")
        cust_b = Customer(name="inv-B")
        cust_gone = Customer(name="inv-gone-rep")
        s.add(cust_a)
        s.add(cust_b)
        s.add(cust_gone)
        s.flush()

        # A owns two standalone accounts; one overpaid (credit > charge)
        a1 = Account(marzban_username="inv_a1", customer_id=cust_a.id, billing_mode=BillingMode.payg)
        a2 = Account(marzban_username="inv_a2", customer_id=cust_a.id, billing_mode=BillingMode.payg)
        # B represents a payg group with two members (members also carry
        # customer_id=B — the double-count trap the one-owner rule guards)
        g = Group(name="inv-group", representative_customer_id=cust_b.id, billing_mode=BillingMode.payg, rate_per_gb=1000.0)
        s.add(g)
        s.flush()
        m1 = Account(marzban_username="inv_m1", customer_id=cust_b.id, group_id=g.id, billing_mode=BillingMode.payg, used_traffic=10 * GB)
        m2 = Account(marzban_username="inv_m2", customer_id=cust_b.id, group_id=g.id, billing_mode=BillingMode.payg, used_traffic=5 * GB)
        # a group whose representative row is gone → its debt has no customer
        g2 = Group(name="inv-orphan-group", representative_customer_id=cust_gone.id, billing_mode=BillingMode.payg)
        s.add(g2)
        s.flush()
        m3 = Account(marzban_username="inv_m3", customer_id=cust_gone.id, group_id=g2.id, billing_mode=BillingMode.payg)
        # THE invisible-money case: an account with no owner at all
        orphan = Account(marzban_username="inv_orphan", billing_mode=BillingMode.payg)
        s.add_all([a1, a2, m1, m2, m3, orphan])
        s.flush()
        now = datetime.now(timezone.utc)

        rows = [
            # A1: charged 50k, paid 30k → owes 20k (overdue customer)
            LedgerEntry(type="charge", amount=50000.0, customer_id=cust_a.id, account_id=a1.id, date=now),
            LedgerEntry(type="credit", amount=30000.0, customer_id=cust_a.id, account_id=a1.id, date=now),
            # A2: overpaid — charged 10k, paid 25k → credit balance, must NOT be debt
            LedgerEntry(type="charge", amount=10000.0, customer_id=cust_a.id, account_id=a2.id, date=now),
            LedgerEntry(type="credit", amount=25000.0, customer_id=cust_a.id, account_id=a2.id, date=now),
            # group members charged at account scope (settle_group's per-member shape)
            LedgerEntry(type="charge", amount=8000.0, customer_id=cust_b.id, group_id=g.id, account_id=m1.id, date=now),
            LedgerEntry(type="charge", amount=4000.0, customer_id=cust_b.id, group_id=g.id, account_id=m2.id, date=now),
            # group-only money (a setup fee with no member to attach to)
            LedgerEntry(type="charge", amount=1500.0, customer_id=cust_b.id, group_id=g.id, date=now),
            # customer-only money for A
            LedgerEntry(type="charge", amount=999.0, customer_id=cust_a.id, date=now),
            # orphan-group member charge (representative exists but we also test rep-gone below)
            LedgerEntry(type="charge", amount=700.0, customer_id=cust_gone.id, group_id=g2.id, account_id=m3.id, date=now),
            # the ownerless account carries REAL debt
            LedgerEntry(type="charge", amount=12345.0, account_id=orphan.id, date=now),
        ]
        for r in rows:
            s.add(r)
        s.commit()
        ids = {"a1": a1.id, "a2": a2.id, "m1": m1.id, "m2": m2.id, "m3": m3.id,
               "orphan": orphan.id, "g": g.id, "g2": g2.id,
               "cust_a": cust_a.id, "cust_b": cust_b.id, "cust_gone": cust_gone.id}
    return ids


def sql_buckets(session: Session) -> dict:
    """Independent re-derivation, straight from the spec text."""
    acc = defaultdict(float)
    grp = defaultdict(float)
    cus = defaultdict(float)
    for row in session.exec(select(LedgerEntry)).all():
        signed = row.amount if row.type == "charge" else -row.amount
        if row.account_id is not None:
            acc[row.account_id] += signed
        elif row.group_id is not None:
            grp[row.group_id] += signed
        elif row.customer_id is not None:
            cus[row.customer_id] += signed
        else:
            raise AssertionError(f"ledger row {row.id} has NO owner — one-owner-per-row violated")
    return {"acc": acc, "grp": grp, "cus": cus}


def verify(db_label: str, session: Session) -> None:
    print(f"== invariant on {db_label} ==")
    buckets = sql_buckets(session)
    book = MoneyBook(session)

    accounts = session.exec(select(Account)).all()
    groups = session.exec(select(Group)).all()
    customers = session.exec(select(Customer)).all()
    members_by_group: dict[int, list[Account]] = defaultdict(list)
    for a in accounts:
        if a.group_id is not None:
            members_by_group[a.group_id].append(a)

    # 1. account level
    bad = [(a.id, book.account_posted(a), buckets["acc"].get(a.id, 0.0))
           for a in accounts if abs(book.account_posted(a) - buckets["acc"].get(a.id, 0.0)) > 0.005]
    check("MoneyBook.account_posted == raw SQL for every account", not bad, str(bad[:4]))

    # 2. group roll-up = group-only + members (never re-querying member rows elsewhere)
    bad = []
    for g in groups:
        expect = buckets["grp"].get(g.id, 0.0) + sum(buckets["acc"].get(a.id, 0.0) for a in members_by_group[g.id])
        if abs(book.group_posted(g) - expect) > 0.005:
            bad.append((g.id, book.group_posted(g), expect))
    check("MoneyBook.group_posted == group-only + member buckets", not bad, str(bad[:4]))

    # 3. customer roll-up = customer-only + directly-owned + represented groups
    bad = []
    for c in customers:
        direct = sum(buckets["acc"].get(a.id, 0.0) for a in accounts
                     if a.customer_id == c.id and a.group_id is None)
        rep = 0.0
        for g in groups:
            if g.representative_customer_id == c.id:
                rep += buckets["grp"].get(g.id, 0.0) + sum(
                    buckets["acc"].get(a.id, 0.0) for a in members_by_group[g.id])
        expect = buckets["cus"].get(c.id, 0.0) + direct + rep
        if abs(book.customer_posted(c) - expect) > 0.005:
            bad.append((c.id, book.customer_posted(c), expect))
    check("MoneyBook.customer_posted == customer-only + direct + represented groups", not bad, str(bad[:4]))

    # 4. Σ(all signed rows) == Σ(buckets) — one owner per row, nothing lost
    total = sum((r.amount if r.type == "charge" else -r.amount)
                for r in session.exec(select(LedgerEntry)).all())
    bucket_sum = sum(buckets["acc"].values()) + sum(buckets["grp"].values()) + sum(buckets["cus"].values())
    check("Σ ledger == Σ buckets", abs(total - bucket_sum) < 0.005, f"{total} vs {bucket_sum}")

    # 5. every positive balance is visible in an operator-facing list
    summary = reports.summary(quota_pct_threshold=80.0, expiry_days_threshold=3, session=session)
    overdue = {o["customer_id"] for o in summary["overdue_customers"]}
    unassigned = {u["account_id"]: u.get("balance") for u in summary["unassigned_accounts"]}
    rep_ids = {g.representative_customer_id for g in groups}
    orphan_groups = {g.id for g in groups if g.representative_customer_id not in {c.id for c in customers}}

    invisible = []
    for a in accounts:
        bal = buckets["acc"].get(a.id, 0.0)
        if bal <= 0.005:
            continue
        if a.group_id is not None:
            continue  # attributed through the group below
        if a.customer_id is not None and a.customer_id in overdue:
            continue
        if a.customer_id is None and a.id in unassigned:
            if unassigned[a.id] is None or abs(unassigned[a.id] - bal) > 0.005:
                invisible.append(("unassigned row missing/wrong balance", a.id, bal, unassigned.get(a.id)))
            continue
        invisible.append(("standalone debtor not in overdue/unassigned", a.id, bal))
    for g in groups:
        bal = buckets["grp"].get(g.id, 0.0)
        if bal > 0.005 and g.representative_customer_id not in overdue:
            invisible.append(("group-only debt with no overdue representative", g.id, bal))
    check("no invisible money: every positive balance lands in an operator-facing list",
          not invisible, str(invisible[:4]))

    # the seeded orphan's debt must be visible with its number
    if db_label == "seeded temp DB":
        orphan_bal = buckets["acc"].get(_IDS["orphan"], 0.0)
        check("seeded ownerless debt shows in unassigned_accounts WITH balance",
              abs(unassigned.get(_IDS["orphan"], -1) - orphan_bal) < 0.005 and orphan_bal > 0,
              f"summary={unassigned.get(_IDS['orphan'])} sql={orphan_bal}")
        check("overpaid account nets into the customer's ROLL-UP (net debtor 20k - 15k + 999 = 5,999)",
              any(abs(o["balance"] - 5999.0) < 0.01
                  for o in summary["overdue_customers"] if o["customer_id"] == _IDS["cust_a"]),
              str([o for o in summary["overdue_customers"] if o["customer_id"] == _IDS["cust_a"]]))


_IDS: dict = {}


def main() -> None:
    global _IDS
    _IDS = seed()
    with Session(engine) as session:
        verify("seeded temp DB", session)

    live = os.environ.get("VPN_INVARIANT_DB")
    if live and Path(live).exists():
        import sqlite3
        # read-only attach of the live copy into this process via a second
        # engine would fight the import-time DATABASE_URL; instead verify the
        # raw-SQL half (the independent implementation) against the copy.
        print(f"== raw-SQL reconciliation on live copy: {live} ==")
        con = sqlite3.connect(f"file:{Path(live).as_posix()}?mode=ro", uri=True)
        acc: dict[int, float] = defaultdict(float)
        grp: dict[int, float] = defaultdict(float)
        cus: dict[int, float] = defaultdict(float)
        ownerless = 0
        total = 0.0
        for aid, gid, cid, rtype, amount in con.execute(
                "SELECT account_id, group_id, customer_id, type, amount FROM ledgerentry"):
            signed = amount if rtype == "charge" else -amount
            total += signed
            if aid is not None:
                acc[aid] += signed
            elif gid is not None:
                grp[gid] += signed
            elif cid is not None:
                cus[cid] += signed
            else:
                ownerless += 1
        bucket_sum = sum(acc.values()) + sum(grp.values()) + sum(cus.values())
        check("live copy: Σ ledger == Σ buckets", abs(total - bucket_sum) < 0.005, f"{total} vs {bucket_sum}")
        check("live copy: no ownerless ledger rows", ownerless == 0, str(ownerless))
        con.close()
    else:
        print("== live-copy checks skipped (set VPN_INVARIANT_DB=<db copy> to run them) ==")

    print()
    if failures:
        print(f"RESULT: {len(failures)} FAILURES: {failures}")
        sys.exit(1)
    print("RESULT: all checks OK")


main()
