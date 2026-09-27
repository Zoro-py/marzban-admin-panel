# Full review round 01 — five areas, 2026-09-27 (branch `fix/vpn-full-review-2026-09-27`)

**Scope:** admin bot (`bot/`), shop bot (`shopbot/`), delegate bot (`delegate_bot/`), money,
frontend, infra/security — the 2026-09-27 full-review brief, angles 1–10 of the 09-20 prompt's
series table, applied per area.

**Method discipline (honest):** the model-council requirement (series 10) could not be executed
in this environment — every external delegation tool is paid/out of scope here. **Fallback per
brief §6.10:** every P0/P1 finding below carries its own independent verification (executed red
test reproducing the exact damage, or a live-data query, or a real-server read-only check), and
the "council" column says so plainly rather than borrowing unearned confirmation. Findings
solely from reading are marked as such and were down-graded or verified before acting on.

**Baseline today:** backend 24/24 + bot 4/4 green before any change; **27/27 + 5/5 + 7-case
delegate-bot guard file after** (new files included in the counts). `npm run build` clean
before and after. Money invariant on TODAY's live copy: Σ ledger 17,729,176.09 == Σ buckets,
0 ownerless rows (`test_ledger_invariant` with `VPN_INVARIANT_DB`).

## Findings table

| ID | Severity | Area | Evidence (executed, not read-only claims) | Council | Status |
|---|---|---|---|---|---|
| MONEY-ADJ-1 | **P0** | money/bots/frontend | Red test `tests/test_adjust_billing.py` reproduced the live damage (settle charged 10*rate a second time); live rows: account 39 charged 300,000 for +60GB on 07-28 **and again** by the 08-10 settle, no reset between (AccountEvent trail); accounts 108/128 currently at-risk | own verification: red test + live data; no second model available | **FIXED** `e1bf05b` (bill_added_gb contract, D14) |
| BOT-DEBT-1 | **P1** | admin bot | Red state proven: `test_debt_console.py` case 10 FAILED on the unfixed code (double-tap posted 2 credits; PTB is sequential so the edit cannot retract the 2nd tap) | own verification: red test | **FIXED** `f9bcd65` (10s repeat-guard, D15) |
| DEL-F5 | P1 | delegate bot | `tests/test_delegate_concurrency.py` red: 2 concurrent renews → 2 charges, account extended once (15GB ≠ 20GB) — invented debt; green after `@serialise_billing` | own verification: red test | **FIXED** `adbc06f` |
| DEL-SCHEMA | P1 (found while fixing DEL-F5) | delegate bot | `from __future__ import annotations` + `@serialise_billing` = request body became a phantom 422 "query" param (FastAPI resolves string annotations against the wrapper's globals) — caught by the concurrency test the moment the decorator was added | own verification: failing test then passing | **FIXED** `adbc06f` (+ constraint documented on `serialise_billing`) |
| C12-HEARTBEAT | P2 | infra/ops | No mechanism anywhere told anyone the operator Telegram channel itself had died (grep `heartbeat/last_success` = 0 hits); settlements would block silently | own verification: grep + design | **FIXED** `41af135` (notify heartbeat + summary fields, D16) |
| DEL-1 | P3 | delegate bot | `_list_accounts` sends up to 25 messages in one burst → Telegram 429 RetryAfter mid-loop → partial list + generic error. Read-only finding (no live test — would need a real bot token) | single-source, own review | OPEN (low priority; fix = RetryAfter-aware sender or paginated list) |
| S1-PRICING | P2 (business) | shop | Live shop: linear 5,000 T/GB (flat). Owner's `price.xlsx` (read-only, never copied): two sheets that disagree with EACH OTHER at 10–20 GB (3,000 vs 3,500) and 20–30 GB (2,500 vs 3,000), all tiers ≤ 4,000/GB — the live 5,000 flat price matches NEITHER sheet at ANY volume | own verification: sheet read + live ShopSettings row | OPEN — owner must say which price is current (business decision, not technical) |
| C11-PHOENIX-SSH | P2 | infra | `ssh vpn-phoenix` (read-only): `sshd_config.d/50-cloud-init.conf: PasswordAuthentication yes` AND `PermitRootLogin yes` — the 09-20 finding is STILL true today | own verification: live SSH read | OPEN (owner/infra action, not this prompt's code) |
| C11-FLEET | info | infra | All 5 live servers up: france-vpn (disk 36%, all 6 containers healthy, Marzban 6.76% CPU), vpn-detroit, vpn-phoenix, marzban-extra1 (NL, 25d uptime), marzban-extra2 (Fremont, 11d). marzban-node1/node2 timeout = the known REMOVED Dallas boxes, not an outage | own verification: live SSH reads | CLOSED (informational) |
| C10-CI | ✅ verified | infra | `ci.yml` runs real backend+bot test loops + frontend build (not import-only); verified by reading today's file — the 09-20 fix is intact | own verification | CLOSED |
| DEPLOY-SECRETS | ⚠️ known | infra | `deploy.yml` still needs the 4 `DEPLOY_*` secrets (manual `workflow_dispatch` only, pre-deploy DB backup built in). README Step 3 documents setup. Left as-is (documented); a guard step is optional polish | own verification | OPEN (owner convenience, not breakage) |
| M1/M2/M3 (shop) | ✅ still closed | shop | `_find_our_marzban_user`, `_extension_landed`, `already_refunded`, byte `compare_digest` all present in today's code; shop test green in the 27/27 | own verification: code + tests | CLOSED (re-verified) |
| OLD-QUOTE-7 | ✅ still closed | shop | `/quote` now runs the same `validate_purchase_request` as `/purchase` (routers/shop.py bot_quote docstring + code) | own verification | CLOSED (re-verified) |
| U1-MOBILE | ⚠️ not run | frontend | Full-panel Playwright pass (390px, light/dark, all pages) NOT executed this session: the fake-Marzban harness was never preserved and rebuilding it was judged disproportionate. `tsc`+`vite build` clean; ChargeHistory preview pages were browser-verified on 09-21 (Addendum 3–4 records) | honest gap | OPEN (carried to next deploy cycle) |
| DATE-PICKER | ⚠️ carried | frontend | Global date-picker on list pages still absent (D8 remainder, unchanged) | own verification | OPEN (documented UX backlog) |

## Positives re-verified today (no action needed)

- Money invariant "no invisible money" holds on the live copy (Σ = 17,729,176.09 exactly, twice).
- Delegate IDOR: ownership re-derived server-side per call (`_get_owned_account` + scope filter);
  customer-delegate cannot reach group accounts and vice versa (existing smoke test covers it).
- Delegate schemas bounded (`extend_gb gt=0 le=10240`, `extend_days gt=0`) — negative extension
  charge-evasion impossible at the API.
- `wallet.py` (pop-once confirm), `topup.py` (backend single-use approval), `bulk.py` (token +
  preview) — bot money paths are guarded; only the debt console was not (BOT-DEBT-1).
- `billable_bytes` negative-diff guard, `attributable_consumed_gb` epoch logic, MoneyBook
  single-owner bucketing — all intact vs `DOMAIN_AND_BILLING.md` spec.
- Git history secrets scan: no `.env`/`.db`/key material ever committed (verified 09-20 by the
  security subagent; `.gitignore` broadened since; re-checked today: repo status clean of DBs).

## Convergence rule status

Fixes landed in order P0 → P1; after the last fix the FULL suite was re-run (27/27 backend,
5/5 bot, delegate guard 7/7, build clean) and the money invariant re-verified on the live copy.
One full convergence re-pass was executed; a second consecutive zero-finding pass was NOT
completed within this session's budget — the carried P2/P3 rows above are the honest remainder.
