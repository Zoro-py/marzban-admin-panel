from datetime import datetime, timezone

from sqlalchemy import bindparam, text
from sqlmodel import SQLModel, Session, create_engine

from app.config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, echo=False, connect_args=connect_args)


def _run_lightweight_migrations() -> None:
    """No formal migration framework for a project this size — `create_all` only
    creates tables that don't exist yet, so a column added to models.py after a
    table already exists on a deployed server needs to be patched in by hand.
    Safe to run on every startup: each check is a no-op once the column exists."""
    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(account)"))}
        if existing and "billing_mode" not in existing:
            conn.execute(text("ALTER TABLE account ADD COLUMN billing_mode VARCHAR NOT NULL DEFAULT 'prepay'"))

        existing_group = {row[1] for row in conn.execute(text("PRAGMA table_info(\"group\")"))}
        if existing_group and "billing_mode" not in existing_group:
            # Default existing groups to 'payg' (not 'prepay') on backfill — every
            # group created before this column existed was, by the original design,
            # a pay-as-you-go group; this keeps their behavior unchanged.
            conn.execute(text("ALTER TABLE \"group\" ADD COLUMN billing_mode VARCHAR NOT NULL DEFAULT 'payg'"))

        if existing and "billed_data_limit" not in existing:
            conn.execute(text("ALTER TABLE account ADD COLUMN billed_data_limit INTEGER NOT NULL DEFAULT 0"))

        if existing and "online_at" not in existing:
            conn.execute(text("ALTER TABLE account ADD COLUMN online_at DATETIME"))

        if existing and "first_seen_traffic" not in existing:
            conn.execute(text("ALTER TABLE account ADD COLUMN first_seen_traffic INTEGER NOT NULL DEFAULT 0"))
            conn.execute(text("ALTER TABLE account ADD COLUMN first_seen_traffic_at DATETIME"))
            # Backfill: for accounts that already existed before this column did, we
            # have no record of their traffic at "first seen" — the safe choice is to
            # start the observation window NOW (baseline = current lifetime total),
            # not to guess. This makes every pre-existing account correctly report
            # "insufficient data" until it accumulates a few real days of *observed*
            # growth, rather than resurrecting the exact misattribution bug this
            # column exists to prevent.
            now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
            conn.execute(
                text(
                    "UPDATE account SET first_seen_traffic = lifetime_used_traffic, "
                    "first_seen_traffic_at = :now WHERE first_seen_traffic_at IS NULL"
                ),
                {"now": now},
            )

        # Real billing bug, not a schema change: usage_baseline (what billable
        # usage is measured FROM) was never initialized when an account was
        # first discovered — it defaulted to 0, so the very first settle for
        # any account with real pre-existing Marzban history would bill its
        # ENTIRE lifetime usage, not just usage since this dashboard started
        # tracking it. usage_baseline_at IS NULL unambiguously identifies
        # accounts that have never been through a settle/reset AND predate the
        # fix (every account created after the fix gets usage_baseline_at set
        # immediately) — same safe policy as first_seen_traffic above: start
        # the baseline now rather than guess, so nothing gets retroactively
        # billed for history this system was never tracking.
        now2 = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
        conn.execute(
            text(
                "UPDATE account SET usage_baseline = lifetime_used_traffic, "
                "usage_baseline_at = :now WHERE usage_baseline_at IS NULL"
            ),
            {"now": now2},
        )

        # Correction to the fix directly above: it turned out to be too
        # conservative. It assumed pre-existing usage MIGHT already have been
        # paid for outside this system, so it excluded ALL history up to the
        # moment it ran from billing — but for an operator whose whole reason
        # for adopting this dashboard was billing they'd been doing by hand,
        # that history was real, unpaid, and needed to show up as debt, not
        # get silently zeroed out. Reset usage_baseline back to 0 for every
        # account that has genuinely never been billed through this system —
        # this does NOT charge anyone by itself (settling is still a separate,
        # explicit operator action); it only corrects the "pending" preview to
        # reflect the true unbilled amount so the operator can decide what to
        # do with it. "Never been billed" is determined per account:
        #   - a group member: the group has never been settled
        #     (Group.last_settled_at IS NULL)
        #   - standalone (no group): no charge-type ledger entry has ever
        #     referenced this account_id (covers settle, reset-with-charge,
        #     and manual invoices alike — any of those means it's already
        #     been billed at least once, so its baseline is left alone)
        # Self-limiting, not a one-shot flag: once a group is genuinely
        # settled or a standalone account is genuinely charged, the
        # corresponding WHERE condition stops matching it, so this becomes a
        # no-op for that account on every subsequent startup.
        never_settled_group_ids = {
            row[0] for row in conn.execute(text('SELECT id FROM "group" WHERE last_settled_at IS NULL'))
        }
        ever_charged_account_ids = {
            row[0]
            for row in conn.execute(
                text("SELECT DISTINCT account_id FROM ledgerentry WHERE type = 'charge' AND account_id IS NOT NULL")
            )
        }
        accounts_to_rebaseline = [
            row[0]
            for row in conn.execute(text("SELECT id, group_id FROM account"))
            if (row[1] in never_settled_group_ids if row[1] is not None else row[0] not in ever_charged_account_ids)
        ]
        if accounts_to_rebaseline:
            conn.execute(
                text("UPDATE account SET usage_baseline = 0 WHERE id IN :ids").bindparams(
                    bindparam("ids", expanding=True)
                ),
                {"ids": accounts_to_rebaseline},
            )

        # Billing basis changed from lifetime_used_traffic to used_traffic (see
        # Account.usage_baseline's docstring) — used_traffic matches exactly
        # what Marzban itself shows as an account's current usage, which is
        # what an operator looking at both screens side by side expects to
        # see reflected here; lifetime_used_traffic (a separate, still-shown
        # "all-time total" figure) survives Marzban resets but doesn't match
        # what Marzban displays as the primary number. This needs a genuine
        # one-time migration, not a self-limiting WHERE clause like the fixes
        # above: an account that's ALREADY been settled has a usage_baseline
        # value that was captured against the OLD field (lifetime_used_traffic)
        # and is meaningless compared against the NEW one (used_traffic) — it
        # must be re-baselined to used_traffic exactly once, and never again
        # (re-applying "baseline = used_traffic now" on every later startup
        # would silently erase any real usage accrued between a genuine settle
        # and the next restart). Never-settled accounts need no action here:
        # their baseline is already 0 from the fix above, which is correct
        # under either field (0 means "everything since account creation is
        # billable" regardless of which counter that's measured against).
        conn.execute(text("CREATE TABLE IF NOT EXISTS _migration_marker (key VARCHAR PRIMARY KEY, applied_at DATETIME)"))
        already_rebaselined = conn.execute(
            text("SELECT 1 FROM _migration_marker WHERE key = 'used_traffic_billing_basis'")
        ).first()
        if not already_rebaselined:
            settled_group_ids = {
                row[0] for row in conn.execute(text('SELECT id FROM "group" WHERE last_settled_at IS NOT NULL'))
            }
            settled_account_ids = [
                row[0]
                for row in conn.execute(text("SELECT id, group_id FROM account"))
                if (row[1] in settled_group_ids if row[1] is not None else row[0] in ever_charged_account_ids)
            ]
            if settled_account_ids:
                conn.execute(
                    text("UPDATE account SET usage_baseline = used_traffic WHERE id IN :ids").bindparams(
                        bindparam("ids", expanding=True)
                    ),
                    {"ids": settled_account_ids},
                )
            now3 = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
            conn.execute(
                text("INSERT INTO _migration_marker (key, applied_at) VALUES ('used_traffic_billing_basis', :now)"),
                {"now": now3},
            )

        # prepay bills the PACKAGE (data_limit), not usage (see
        # services.billable_bytes) -- billed_data_limit tracks how much of
        # the CURRENT package has already been charged for, same role as
        # usage_baseline plays for payg. It's a brand-new column defaulting
        # to 0 for every row, which would make every prepay account's full
        # package show up as newly pending -- correct for one that's
        # genuinely never been charged, wrong for one already paid via a
        # manual invoice (or a group settle) before this column existed. Same
        # "never billed vs already billed" reasoning as the usage_baseline
        # fix above, reusing ever_charged_account_ids: a genuinely
        # never-billed account (or one in a never-settled group) is left at 0
        # (its full package correctly shows as pending); one that's already
        # been charged is set to its current data_limit (nothing further
        # pending until the package grows). Self-limiting: once set to a
        # nonzero value here, or by an actual settle, this stops matching it.
        settled_group_ids = {
            row[0] for row in conn.execute(text('SELECT id FROM "group" WHERE last_settled_at IS NOT NULL'))
        }
        already_billed_accounts = [
            row
            for row in conn.execute(text("SELECT id, group_id, data_limit FROM account WHERE billed_data_limit = 0"))
            if (row[1] in settled_group_ids if row[1] is not None else row[0] in ever_charged_account_ids)
        ]
        for account_id, _group_id, data_limit in already_billed_accounts:
            conn.execute(
                text("UPDATE account SET billed_data_limit = :dl WHERE id = :aid"),
                {"dl": data_limit or 0, "aid": account_id},
            )

        # "Every account belongs to one person unless it's deliberately
        # grouped" -- customer_id was being treated as an optional admin step
        # (assign a customer before an account can be billed), leaving every
        # account sync discovers unowned until someone manually links one.
        # The operator's actual model is the reverse: an account IS its own
        # billing identity by default, a Group is the occasional exception.
        # sync_job.py now provisions a personal Customer for every NEW
        # account it discovers going forward; this backfills the accounts
        # that were already sitting unowned before that changed. Self-limiting
        # like the fixes above: once customer_id is set (here, or later by an
        # operator explicitly clearing it back to unassigned), this stops
        # matching that account.
        unowned = conn.execute(
            text("SELECT id, marzban_username FROM account WHERE customer_id IS NULL AND group_id IS NULL")
        ).all()
        if unowned:
            now4 = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
            for account_id, username in unowned:
                result = conn.execute(
                    text("INSERT INTO customer (name, is_group_rep, created_at) VALUES (:name, 0, :now)"),
                    {"name": username, "now": now4},
                )
                conn.execute(
                    text("UPDATE account SET customer_id = :cid WHERE id = :aid"),
                    {"cid": result.lastrowid, "aid": account_id},
                )


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    if settings.database_url.startswith("sqlite"):
        _run_lightweight_migrations()


def get_session():
    with Session(engine) as session:
        yield session
