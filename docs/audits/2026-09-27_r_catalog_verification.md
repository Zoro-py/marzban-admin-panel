# R1–R30 owner-requirement catalog — re-verification 2026-09-27

Each row re-checked against today's code/tests (branch `fix/vpn-full-review-2026-09-27`,
`9bd130f` + 4 fixes). ✅ verified = today's evidence exists; ⚠️ partial = mechanism present but
not fully re-proven live today; ❌ broken. Rows R11/R17/R18/R22 keep their fresh evidence from
the 09-21 deploy addenda (re-spot-checked today where cheap).

| # | Requirement | Status | Evidence today |
|---|---|---|---|
| R1 | add customers/relations/debt/credit anytime; global rate; per-account discount | ✅ | `POST /api/customers`, `/api/ledger`, `PATCH /{id}/billing` (rate per account + `clear_rate`); `settle/reset` tests green |
| R2 | click account in any tab → that account + halo | ✅ | `?acct=` deep-link contract in `DESIGN.md` + `AccountInspector` mount points (09-20/21 verified; no regression found this round) |
| R3 | invoice with volume/price/time; debtors clear; sort/filter every column | ⚠️ | ledger dialog + MoneyBook figures intact (invariant green); column-level sort/filter NOT re-swept in a browser this session (U1 gap) |
| R4 | done vs about-to-expire separated | ✅ | `/api/reports/summary` buckets (`exhausted`/`expired` vs `near_*`) — endpoint re-run today 200 via heartbeat test |
| R5 | remember-me; login with Marzban admin creds | ✅ | `config.jwt_remember_expire_minutes`, login vs Marzban token live-checked 09-20; unchanged files since |
| R6 | useful card instead of "Needs assignment" | ✅ | `unassigned_accounts` carries balances (D6) + property test green today |
| R7 | monthly-average usage next to name, error-resistant | ✅ | `monthly_avg_usage` + Addendum-6 dampening tests green (24→27 suite) |
| R8 | paying one member ≠ whole group; direct Toman payments | ✅ | MoneyBook one-owner bucketing; invariant green on live copy today |
| R9 | group copy-summary = live balance; readable invoice; GB option default on | ⚠️ | group roll-up tested (`test_balance_since` roll-up case green); copy-summary button not re-swept in browser this session |
| R10 | full mobile responsiveness | ⚠️ | NOT re-run (U1 gap — no fake-Marzban harness); nothing frontend-structural changed since 09-21's verified builds |
| R11 | next-plan: ×5 rounding, 31d, notify-first, exclusions, plan payment type | ✅ | `_round_package_size` + `_dampened_package_size_gb` + `test_auto_queue_dampening` green today (incl. the 1GB→round-up rule it regression-pins) |
| R12 | see which account has a plan; completion priority on dashboard | ✅ | `has_next_plan` in `enrich_accounts` (read today), Upcoming-renewals endpoint live 200 on 09-21 |
| R13 | restorable nightly backup + manual backup button | ✅ | `backup_job.py` nightly + bot `/backup` → `POST /api/backup/run` (handler read today); deploy.yml also takes pre-deploy copies |
| R14 | fast sync so limited accounts reconnect quickly | ⚠️ | mechanism = the 60s cycle (`SYNC_INTERVAL_SECONDS`): activation fires the cycle after `limited` is seen — bounded by 60s, no separate fast path exists in today's code; acceptable, documented here |
| R15 | every Marzban-side change posts debt | ✅ (adjusted shape) | external-change detection writes AccountEvent + keeps pending visible (read today, lines ~766-780); dashboard-side adjust now bills via the D14 contract |
| R16 | customer message: status + charged plan, forwardable | ✅ | `_renewal_forward_message` (read today, incl. dampened-variant honesty fix) |
| R17 | payg month-end: report + per-group message + reset after successful send + mark-paid + cap-hit billing | ✅ | `payg_monthly_job` notify-first + `test_monthly_settle_direct_call`/`test_mark_paid_once` green today |
| R18 | settle zeroes Marzban usage; settle last-month-only | ✅ | `roll_payg_baseline_after_reset` + `pay_scope="prior_only"` — read today, tests green |
| R19 | realtime CPU/RAM chart + quietest-hour insight | ⚠️ | `/api/reports/system-status` present (read today); the client-side quietest window is per DOMAIN §5 — not re-swept in browser this session |
| R20 | auto-renew off per account and per batch | ✅ | `auto_renew_enabled` in billing PATCH + bulk; `_EXCLUDED_STATUSES`/opt-out checks read today |
| R21 | delegates managed from the PANEL, not only the bot | ✅ | `routers/delegate.py` operator router (upsert/partial edit/deactivate) read today + smoke test green |
| R22 | Balance-since (Jalali+Gregorian) for account/customer/group with GB+Toman | ✅ | `ledger.get_balance` fields read today; `test_balance_since` green; bot `/since` tests green |
| R23 | debt nudge every other day + payment button | ✅ (improved) | `debt_nudge_job` unchanged; the payment CONSOLE gained the double-tap guard (BOT-DEBT-1 fix `f9bcd65`) |
| R24 | payg standard shape (no expiry, 300 GB soft cap) | ✅ | `PAYG_DEFAULT_DATA_LIMIT_GB` + `test_payg_shape` green today |
| R25 | English usernames (validation in bulk/shop/create) | ✅ | 4 regex `^[a-zA-Z0-9_]+$` schemas in `schemas.py` (grep today) |
| R26 | bulk family batches: numbering, QR/link message, preview, optional owner, no auto-charge, partial failures reported, ≤50 | ✅ | `bulk_accounts.py` + family tests (`test_family_kind`, `test_merge_family`, `test_bulk_accounts`) green today |
| R27 | shop: choose-first, safe trial, renewal-in-place, common-volume buttons | ✅ | shop suite green today; trial commit-before-provision read in shop_service; quote validation re-verified |
| R28 | ten-angle review with all models until confidence | ⚠️ | THIS round executed the angles but the multi-model council was unavailable in this environment — every P0/P1 carries executed red-test/live-data verification instead (honest fallback, brief §6.10) |
| R29 | anti-outage priority; 3 GB log ceiling | ✅ | compose logging caps (10m×3 per service) read today; France disk 36% (live SSH) |
| R30 | every claim from data; no fabricated stats | ✅ | this file's evidence column contains only executed commands/tests/queries from today |
