"""One-off remediation for the «billed_data_limit bomb» (docs/DECISIONS.md D21).

Neutralizes the stale `billed_data_limit` baseline on live payg-shaped
accounts whose leftover baseline would read as phantom sold-package debt the
moment their effective billing mode flips payg->prepay (prepay's
billable_bytes = data_limit - billed_data_limit). Live root case: Mahan
(id=8), 2026-09-28 — one group assignment jumped owes-now ~154k -> ~1,425k.

The class is EXACTLY the measured spec query (2026-09-28):

    data_limit >= 300GB (the payg standard-shape Marzban cap)
    AND 1 <= billed_data_limit <= 250GB (a stale prepay leftover, not 0)
    AND deleted_at IS NULL
    AND COALESCE(group.billing_mode, account.billing_mode) = 'payg'

The write itself is NOT a parallel hand-rolled UPDATE: it calls
app.services.neutralize_billed_baseline() — the same function the two new
mode-flip guards call — once per in-scope account, so the remediation and
the shipped code cannot drift apart. It creates no LedgerEntry (the helper
writes billed_data_limit plus an AccountEvent audit row, nothing else).

Usage:
    python neutralize_stale_billed_baselines.py --db <path/to/vpn.db>           # dry-run (read-only)
    python neutralize_stale_billed_baselines.py --db <path/to/vpn.db> --apply   # real write, one transaction

Dry-run opens the DB read-only (mode=ro) and cannot write even by accident.
--apply performs: pre-transaction full-row snapshot -> fresh in-transaction
class selection -> helper call per account -> one commit -> verification
(class query returns 0 rows, total ledger sum identical, every account
column diffed before/after with ONLY billed_data_limit allowed to differ,
touched ids == in-scope ids).

Runs both locally against a copy of the live DB and inside the backend
container: the app import resolves against (in order) this script's own
directory (container layout: script next to an app/ bundle) and the repo's
backend/ directory (local checkout layout).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
for candidate in (SCRIPT_DIR, SCRIPT_DIR.parent / "backend"):
    if (candidate / "app").is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from app.models import Account, BillingMode  # noqa: E402
from app.services import GB, billable_bytes, neutralize_billed_baseline  # noqa: E402

# The spec query, verbatim in its WHERE semantics (GB columns are computed
# in Python below so the same SQL drives every check here).
CLASS_SQL = """
SELECT a.id, a.marzban_username, a.group_id,
       COALESCE(g.billing_mode, a.billing_mode) AS effective_mode,
       a.data_limit, a.billed_data_limit
FROM account a LEFT JOIN "group" g ON a.group_id = g.id
WHERE a.data_limit >= 300*1073741824
  AND a.billed_data_limit BETWEEN 1 AND 250*1073741824
  AND a.deleted_at IS NULL
  AND COALESCE(g.billing_mode, a.billing_mode) = 'payg'
"""

ACCOUNT_TABLE = "account"
LEDGER_TABLE = "ledgerentry"


def gb(value) -> float:
    return round((value or 0) / GB, 3)


def raw_connect(db_path: str, read_only: bool = True) -> sqlite3.Connection:
    uri = f"file:{Path(db_path).as_posix()}" + ("?mode=ro" if read_only else "")
    con = sqlite3.connect(uri, uri=True, timeout=30.0)
    con.row_factory = sqlite3.Row
    return con


def ledger_sum(con: sqlite3.Connection):
    return con.execute(f"SELECT COALESCE(SUM(amount), 0) FROM {LEDGER_TABLE}").fetchone()[0]


def select_class(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(CLASS_SQL).fetchall()


def account_row(con: sqlite3.Connection, account_id: int) -> sqlite3.Row:
    return con.execute(f"SELECT * FROM {ACCOUNT_TABLE} WHERE id = ?", (account_id,)).fetchone()


def diff_rows(before: sqlite3.Row, after: sqlite3.Row) -> list[str]:
    return [k for k in before.keys() if before[k] != after[k]]


def phantom_billable_gb(row) -> float:
    """What prepay's formula would read TODAY if the mode flipped without the
    fix — the phantom debt this row carries around."""
    ghost = Account(
        id=row["id"], marzban_username=row["marzban_username"], group_id=row["group_id"],
        data_limit=row["data_limit"], billed_data_limit=row["billed_data_limit"],
        used_traffic=0, usage_baseline=0,
    )
    return gb(billable_bytes(ghost, BillingMode.prepay))


def print_class(rows, title: str) -> None:
    print(f"\n=== {title}: {len(rows)} row(s) ===")
    print(f"{'id':>4}  {'username':<18} {'grp':>4} {'mode':<6} {'data_gb':>9} {'billed_gb':>10} {'phantom_if_flipped_gb':>22}")
    for row in rows:
        print(f"{row['id']:>4}  {row['marzban_username']:<18} {str(row['group_id']):>4} {row['effective_mode']:<6} "
              f"{gb(row['data_limit']):>9} {gb(row['billed_data_limit']):>10} {phantom_billable_gb(row):>22}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="path to vpn.db (live file or a copy)")
    parser.add_argument("--apply", action="store_true", help="perform the writes (default: dry-run, read-only)")
    args = parser.parse_args()

    con = raw_connect(args.db, read_only=True)
    ledger_before = ledger_sum(con)
    rows = select_class(con)
    print_class(rows, "DRY-RUN (read-only)" if not args.apply else "PRE-APPLY CLASS QUERY")
    print(f"ledger total: {ledger_before}")
    if not args.apply:
        con.close()
        print("\nDry-run complete — nothing was written. Re-run with --apply to perform the remediation.")
        return 0

    # ── Apply ────────────────────────────────────────────────────────────────
    from sqlmodel import Session, create_engine, text

    in_scope_ids = {row["id"] for row in rows}
    # Full-row snapshot BEFORE the transaction, so the post-commit diff proves
    # exactly which columns moved (a connection opened after the commit would
    # only see the new state and could not diff at all).
    before_full = {aid: account_row(con, aid) for aid in sorted(in_scope_ids)}

    engine = create_engine(f"sqlite:///{Path(args.db).as_posix()}", connect_args={"timeout": 30.0})
    changed: list[int] = []
    event_details: list[str] = []
    with Session(engine) as session:
        # Fresh, in-transaction class selection: the write list is decided
        # NOW, not inherited from the possibly-stale dry-run.
        fresh = {r.id: r for r in session.execute(text(CLASS_SQL)).fetchall()}
        if set(fresh) != in_scope_ids:
            print(f"ABORT: class membership changed between read and apply: "
                  f"appeared={sorted(set(fresh) - in_scope_ids)} disappeared={sorted(in_scope_ids - set(fresh))}")
            con.close()
            return 1
        for account_id in sorted(fresh):
            account = session.get(Account, account_id)
            if account is None:
                print(f"ABORT: account {account_id} vanished mid-transaction")
                con.close()
                return 1
            before_value = account.billed_data_limit
            if neutralize_billed_baseline(
                session, account,
                reason="one-off live remediation of stale payg-phase baseline (D21)",
                created_by="remediation:billed-baseline-2026-09-28",
            ):
                changed.append(account_id)
                event_details.append(f"id={account_id} {account.marzban_username}: "
                                     f"{before_value} -> {account.billed_data_limit} bytes "
                                     f"({gb(before_value)} -> {gb(account.billed_data_limit)} GB)")
        session.commit()

    # ── Verification, all through a fresh read-only connection ──────────────
    verify = raw_connect(args.db, read_only=True)
    ledger_after = ledger_sum(verify)
    after_class = select_class(verify)
    problems: list[str] = []

    if after_class:
        problems.append(f"class query still returns {len(after_class)} row(s): {[dict(r) for r in after_class]}")
    if ledger_before != ledger_after:
        problems.append(f"ledger total changed: {ledger_before} -> {ledger_after}")

    for account_id in sorted(in_scope_ids):
        after_row = account_row(verify, account_id)
        if after_row is None:
            problems.append(f"account {account_id} disappeared")
            continue
        diffs = diff_rows(before_full[account_id], after_row)
        expected_target = before_full[account_id]["data_limit"] or 0
        if diffs == ["billed_data_limit"] and after_row["billed_data_limit"] == expected_target:
            print(f"row {account_id:>4} {after_row['marzban_username']:<18} billed_data_limit "
                  f"{gb(before_full[account_id]['billed_data_limit'])} -> {gb(after_row['billed_data_limit'])} GB "
                  f"— ONLY billed_data_limit changed")
        elif diffs == []:
            print(f"row {account_id:>4}: unchanged (baseline was already current)")
        else:
            problems.append(f"account {account_id}: unexpected column diffs {diffs} "
                            f"(billed now {after_row['billed_data_limit']}, expected {expected_target})")

    print_class(after_class, "POST-APPLY CLASS QUERY (must be empty)")
    print(f"ledger total: {ledger_before} -> {ledger_after}")
    if changed:
        print(f"neutralized now: {len(changed)} row(s) -> ids {sorted(changed)}")
        for detail in event_details:
            print(f"  AccountEvent: {detail}")
    if problems:
        print("\nVERIFICATION FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        con.close()
        verify.close()
        engine.dispose()
        return 1
    print(f"\nOK: {len(changed)} account(s) neutralized, 0 class rows remain, ledger total identical "
          f"({ledger_after}), no LedgerEntry created.")
    con.close()
    verify.close()
    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
