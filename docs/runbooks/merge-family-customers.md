# Runbook — fold one-account customers into one family (khanevadeh1..12 → khanevadeh)

**Money moved: none.** The tool moves account ownership and re-points ledger `customer_id`; it aborts
and rolls back if the whole-ledger total, the family's posted balance or the account count differ.
**Do this only after the version with `Customer.kind` is deployed** (its startup migration adds the
column; the script refuses to run without it).

Owner-run, on the server (`ssh france-vpn`), from `/opt/marzban-admin-panel`. The DB lives in the
backend's Docker volume, so run the script inside the backend container:

```bash
cd /opt/marzban-admin-panel
docker compose cp scripts/merge_family_customers.py backend:/tmp/merge_family_customers.py
# 1) DRY RUN — prints the plan + invariants, changes nothing:
docker compose exec backend python /tmp/merge_family_customers.py --db /app/vpn.db --base khanevadeh
# 2) Read the output: 12 customers, 12 accounts, ledger_total_before == ledger_total_after,
#    family_posted_before == family_posted_after. Anything under SKIPPED stays untouched.
# 3) Take a fresh panel backup first (POST /api/backup/run, or the bot's /backup), then apply:
docker compose exec backend python /tmp/merge_family_customers.py --db /app/vpn.db --base khanevadeh --apply
```

(Adjust `--db` to the real path: `docker compose exec backend sh -c 'echo $DATABASE_URL'`.) `--apply`
also writes `<db>.pre_family_merge_<timestamp>` next to the DB before committing.

**Verify after:** Customers page → «Families only» shows `khanevadeh` with 12 accounts; dashboard
"Customers in debt" shows ONE `khanevadeh` row (was 12); `/debts` in the bot lists it under
«در جریان».

**Rollback:** stop the backend (`docker compose stop backend`), copy the
`.pre_family_merge_<timestamp>` file back over the DB file, start it again. Nothing else depends on
the merge (ledger rows keep their `account_id`).

Dry-run evidence (2026-09-21, copy of the live DB migrated with the new code): 12 customers →
1, ledger total 14,027,173.31 before and after, family net 1,050,000 (150,000 posted + 900,000
pending) before and after via `MoneyBook`.
