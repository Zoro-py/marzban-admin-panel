#!/usr/bin/env python3
"""Merge one-account customers into a single family customer.

Why: sync used to adopt every unknown Marzban user as its OWN personal
customer, so a family batch («khanevadeh1» … «Khanevadeh12») became twelve
one-account customers — twelve rows in every debt list instead of one payer.
This folds them into one customer with kind='family' by moving each source
customer's accounts and ledger rows to the target and deleting the emptied
source customers.

Safety, in the order the script enforces it:

  1. DRY-RUN IS THE DEFAULT. It performs the whole merge inside a
     transaction, checks the invariants, prints the report, and ROLLS BACK.
     `--apply` is the only thing that commits.
  2. Money must not move. Before and after, it recomputes (with plain SQL,
     independent of the app's MoneyBook) the whole-ledger total and the
     combined posted balance of the sources vs. the target. Any difference
     aborts and rolls back.
  3. A source customer is only merged if it is clearly a throwaway personal
     record: matches `<base><number>` (case-insensitive), represents no
     group, has no delegate/shop link, and none of its accounts is a group
     member. Anything else is listed as SKIPPED and left alone.
  4. `--apply` first writes a consistent backup next to the DB
     (`<db>.pre_family_merge_<timestamp>`), then commits.

Usage (from the repo root or anywhere; needs only the stdlib):

    python scripts/merge_family_customers.py --db backend/vpn.db --base khanevadeh
    python scripts/merge_family_customers.py --db backend/vpn.db --base khanevadeh --apply

On the server the DB lives in the backend's Docker volume — run it from a
container that mounts that volume (or `docker cp` a backup out, run the
dry-run there, and only `--apply` on the live file in a quiet moment).

Ledger rows keep their own `account_id`, so ownership by account never
changes; `customer_id` on those rows is re-pointed to the target so the
customer-level history views follow the family. Nothing is created in the
ledger and nothing is charged.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timezone


def _signed_sum_sql(where: str) -> str:
    return (
        "SELECT COALESCE(SUM(CASE WHEN type='charge' THEN amount ELSE -amount END), 0) "
        f"FROM ledgerentry WHERE {where}"
    )


def _customer_posted(conn: sqlite3.Connection, customer_id: int) -> float:
    """The customer's own accounts + customer-only rows (no groups — merge
    candidates never represent one). Same attribution rule as MoneyBook:
    an entry belongs to its account if it has one, else its group, else its
    customer."""
    return conn.execute(
        _signed_sum_sql(
            "(account_id IN (SELECT id FROM account WHERE customer_id = ?)) "
            "OR (account_id IS NULL AND group_id IS NULL AND customer_id = ?)"
        ),
        (customer_id, customer_id),
    ).fetchone()[0]


def _sanity_columns(conn: sqlite3.Connection) -> None:
    need = {"customer": {"id", "name", "contact", "kind"}, "account": {"id", "customer_id", "group_id", "marzban_username"},
            "ledgerentry": {"id", "customer_id", "account_id", "group_id", "type", "amount"}}
    for table, cols in need.items():
        have = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        missing = cols - have
        if missing:
            raise SystemExit(
                f"table {table} lacks {sorted(missing)} — deploy the version with Customer.kind first "
                f"(its startup migration adds the column), then rerun."
            )


def merge(conn: sqlite3.Connection, base: str, target_name: str, apply: bool) -> dict:
    """Runs the merge in one transaction. Commits only when `apply` and every
    invariant holds; otherwise rolls back. Returns a report dict."""
    _sanity_columns(conn)
    pattern = re.compile(r"^" + re.escape(base.lower()) + r"(\d+)$")
    report: dict = {"merged": [], "skipped": [], "target": None, "committed": False, "invariants": {}}

    conn.execute("BEGIN IMMEDIATE")
    try:
        total_before = conn.execute(_signed_sum_sql("1=1")).fetchone()[0]

        candidates = [
            (r[0], r[1], r[2] or "") for r in conn.execute("SELECT id, name, contact FROM customer ORDER BY id")
            if pattern.match((r[1] or "").strip().lower())
        ]
        if not candidates:
            raise SystemExit(f"no customer named like '{base}<number>' — nothing to do")

        reps = {r[0] for r in conn.execute('SELECT representative_customer_id FROM "group"')}
        delegate_ids = {r[0] for r in conn.execute("SELECT customer_id FROM delegate WHERE customer_id IS NOT NULL")} \
            if _has_table(conn, "delegate") else set()
        shop_ids = {r[0] for r in conn.execute("SELECT customer_id FROM shopuser WHERE customer_id IS NOT NULL")} \
            if _has_table(conn, "shopuser") else set()

        eligible: list[tuple[int, str, str]] = []
        for cid, name, contact in candidates:
            reasons = []
            if cid in reps:
                reasons.append("represents a group")
            if cid in delegate_ids:
                reasons.append("is a delegate's customer")
            if cid in shop_ids:
                reasons.append("is linked to a shop user")
            in_group = conn.execute(
                "SELECT COUNT(*) FROM account WHERE customer_id = ? AND group_id IS NOT NULL", (cid,)
            ).fetchone()[0]
            if in_group:
                reasons.append(f"{in_group} of its accounts belong to a group")
            if reasons:
                report["skipped"].append({"id": cid, "name": name, "why": "; ".join(reasons)})
            else:
                eligible.append((cid, name, contact))

        if not eligible:
            raise SystemExit("every candidate was skipped — nothing to merge (see the report)")

        # Target: an existing customer with exactly this name (case-insensitive),
        # else a new one. An existing target that is NOT among the eligible
        # sources is allowed (extending a family) but must not itself be a
        # group representative — that would mix group money into the check.
        existing = conn.execute(
            "SELECT id, name, contact, kind FROM customer WHERE lower(trim(name)) = ?", (target_name.strip().lower(),)
        ).fetchone()
        created_target = False
        if existing:
            target_id = existing[0]
            if target_id in reps and target_id not in {c[0] for c in eligible}:
                raise SystemExit(f"target customer '{existing[1]}' represents a group — pick another --name")
        else:
            cur = conn.execute(
                "INSERT INTO customer (name, contact, is_group_rep, kind, created_at) VALUES (?, NULL, 0, 'family', ?)",
                (target_name.strip(), datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")),
            )
            target_id = cur.lastrowid
            created_target = True

        sources = [c for c in eligible if c[0] != target_id]
        source_ids = [c[0] for c in sources]

        # Invariant inputs, measured before any UPDATE.
        posted_before = sum(_customer_posted(conn, c) for c in source_ids) + _customer_posted(conn, target_id)
        accounts_before = conn.execute(
            f"SELECT COUNT(*) FROM account WHERE customer_id IN ({_qs(source_ids + [target_id])})", source_ids + [target_id]
        ).fetchone()[0]

        for cid, name, contact in sources:
            n_acc = conn.execute("SELECT COUNT(*) FROM account WHERE customer_id = ?", (cid,)).fetchone()[0]
            n_led = conn.execute("SELECT COUNT(*) FROM ledgerentry WHERE customer_id = ?", (cid,)).fetchone()[0]
            posted = _customer_posted(conn, cid)
            conn.execute("UPDATE account SET customer_id = ? WHERE customer_id = ?", (target_id, cid))
            conn.execute("UPDATE ledgerentry SET customer_id = ? WHERE customer_id = ?", (target_id, cid))
            report["merged"].append({"id": cid, "name": name, "accounts": n_acc, "ledger_rows": n_led, "posted": round(posted, 2)})

        # Contact: keep the target's; otherwise adopt the first source's non-empty one.
        tgt_contact = conn.execute("SELECT contact FROM customer WHERE id = ?", (target_id,)).fetchone()[0]
        if not tgt_contact:
            first = next((c[2] for c in sources if c[2]), None)
            if first:
                conn.execute("UPDATE customer SET contact = ? WHERE id = ?", (first, target_id))
        conn.execute("UPDATE customer SET kind = 'family' WHERE id = ?", (target_id,))

        # Nothing may still reference a source customer before it is deleted.
        for cid in source_ids:
            refs = conn.execute(
                "SELECT (SELECT COUNT(*) FROM account WHERE customer_id=?1) + (SELECT COUNT(*) FROM ledgerentry WHERE customer_id=?1) "
                '+ (SELECT COUNT(*) FROM "group" WHERE representative_customer_id=?1)',
                (cid,),
            ).fetchone()[0]
            if refs:
                raise SystemExit(f"customer {cid} is still referenced ({refs} rows) — aborting, nothing changed")
        if source_ids:
            conn.execute(f"DELETE FROM customer WHERE id IN ({_qs(source_ids)})", source_ids)

        # Money must not have moved.
        total_after = conn.execute(_signed_sum_sql("1=1")).fetchone()[0]
        posted_after = _customer_posted(conn, target_id)
        accounts_after = conn.execute("SELECT COUNT(*) FROM account WHERE customer_id = ?", (target_id,)).fetchone()[0]
        inv = {
            "ledger_total_before": round(total_before, 2), "ledger_total_after": round(total_after, 2),
            "family_posted_before": round(posted_before, 2), "family_posted_after": round(posted_after, 2),
            "accounts_before": accounts_before, "accounts_after": accounts_after,
        }
        report["invariants"] = inv
        if abs(total_before - total_after) > 0.005 or abs(posted_before - posted_after) > 0.005 or accounts_before != accounts_after:
            raise SystemExit(f"INVARIANT BROKEN — rolled back, nothing changed: {inv}")

        report["target"] = {"id": target_id, "name": target_name.strip(), "created": created_target}
        if apply:
            conn.execute("COMMIT")
            report["committed"] = True
        else:
            conn.execute("ROLLBACK")
        return report
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _qs(items: list) -> str:
    return ",".join("?" for _ in items) or "NULL"


def _print(report: dict, apply: bool) -> None:
    print("\nWILL MERGE" if not apply else "\nMERGED")
    for m in report["merged"]:
        print(f"  #{m['id']:<5} {m['name']:<20} accounts={m['accounts']} ledger_rows={m['ledger_rows']} posted={m['posted']:,.2f}")
    if report["skipped"]:
        print("SKIPPED (left untouched):")
        for s in report["skipped"]:
            print(f"  #{s['id']:<5} {s['name']:<20} {s['why']}")
    t = report["target"]
    print(f"TARGET: #{t['id']} «{t['name']}» ({'new' if t['created'] else 'existing'}, kind=family)")
    print("INVARIANTS:", report["invariants"])
    print("RESULT:", "COMMITTED" if report["committed"] else "dry-run only — rolled back, nothing changed")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True, help="path to the backend SQLite file")
    ap.add_argument("--base", required=True, help="base name of the batch, e.g. khanevadeh (matches <base><number>)")
    ap.add_argument("--name", help="family customer name (default: the base, lowercased)")
    ap.add_argument("--apply", action="store_true", help="commit (default is a rolled-back dry run)")
    args = ap.parse_args(argv)
    target_name = args.name or args.base.lower()

    if args.apply:
        # A consistent snapshot first, via SQLite's own backup API.
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{args.db}.pre_family_merge_{stamp}"
        src = sqlite3.connect(args.db)
        dst = sqlite3.connect(backup_path)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        print(f"backup written: {backup_path}")

    conn = sqlite3.connect(args.db, isolation_level=None)  # explicit BEGIN/COMMIT above
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        report = merge(conn, args.base, target_name, args.apply)
    finally:
        conn.close()
    _print(report, args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
