# Plan — full panel review 2026-09-27 (branch `fix/vpn-full-review-2026-09-27`)

Progress log for the 2026-09-27 full review (brief: `GLM/PROMPT_2026-09-27_vpn_panel_full_review.md`).
Updated every ~25-30 tool calls per brief §6.9.

## Phase 0 — foundation

- [x] Brief read in full; Shiraze-side context paths resolved.
- [x] `git status` clean (untracked: `.claude/`, `docs/from-cursor-repo/` — both left alone).
- [x] Branch `fix/vpn-full-review-2026-09-27` created from `main` @ `9bd130f`.
- [x] Mandatory reads: `AGENTS.md`, `DOMAIN_AND_BILLING.md`, `DECISIONS.md` (D1–D13-BS), `README.md`, `frontend/DESIGN.md`.
- [x] Deploy records: `2026-09-20.md`, `2026-09-21.md` + addenda 1–6 read.
- [x] Audits read: round_01, round_02, charge_history_review, council_round_01, history_vs_group_totals, baseline.
- [x] `docs/from-cursor-repo/subagent_reports/` (4 reports: billing audit 1.1–2.5, shop M1–M4/B1–B2, security review, shop-v2 strategy) — to be re-verified against today's code in Phase 1, not trusted.
- [x] Baseline tests re-run today: **backend 24/24 green**, **bot 4/4 green** (matches Addendum 6's 24 backend + 4 bot).
- [x] Live DB read-only copy: `sqlite3` not present in the backend container → taken via Python `Connection.backup()` from a `mode=ro` source connection inside the container to `/tmp/`, then `docker cp` + `scp` → `backend/vpn_audit_copy_20260927.db` (39,575,552 bytes, `PRAGMA integrity_check` = ok, gitignored).
  - Live counts at copy time: 163 accounts / 155 customers / 329 ledger rows / ledger signed sum **17,729,176.09** Toman.
  - This copy is the baseline for every later query. Nothing on the server was written except the two throwaway files under the container's `/tmp/`.
- Notes: no SSH writes; Marzban untouched; `rescue/orphan-5dcf458` and the `glm/20260927-040344` worktree untouched.

## Phase 1 — parallel audit (5 areas × 10 angles)

- [x] delegate_bot/ + delegate_service + routers/delegate.py read in full; existing smoke test covers IDOR/credit-cap/one-owner.
- [x] Money invariant re-run on TODAY's live copy: Σ=17,729,176.09 == buckets, 0 ownerless rows (test_ledger_invariant with VPN_INVARIANT_DB).
- [x] services.py read in full (MoneyBook/billable_bytes/attributable_consumed_gb/serialise_billing/effective_rate/close_out...). No drift found vs DOMAIN_AND_BILLING.md.
- [x] bot/ admin: bot.py, common, customer, bill, debt (full), wallet, topup, account read. **Finding BOT-DEBT-1 (P1): `debtdo:post` has no double-tap guard and no Idempotency-Key — two sequential taps on the same ✅ confirm button post two credits (PTB processes updates sequentially, so the button stays actionable for the 2nd tap; the ledger is append-only → invented money).** Red test to be added.
- [x] **Finding MONEY-ADJ-1 (P0): adjust (+GB) + immediate charge double-bills.** `/api/accounts/{id}/adjust` grows `data_limit` but never `billed_data_limit`; both the dashboard AdjustSection (default-on checkbox) and bot `/extend <u> <days> <gb>` post an immediate `/api/ledger` charge for the added GB, while pending keeps showing the same GB (data_limit − billed_data_limit) → the NEXT settle bills it AGAIN. Confirmed on live data: account 39 (Benyamin): adjust +60GB charge 300,000 (07-28) → prepay settle 08-10 charged exactly 300,000 again (no reset between; AccountEvent trail confirms). Accounts 108 (+25GB) and 128 (+30GB) are currently in the same at-risk state (charge posted, pending not cleared, settle pending). Fix designed: `bill_added_gb` flag on the adjust request; caller posts the charge FIRST, then adjust with flag=true bumps `billed_data_limit` by the delta (clamped ≤ new data_limit) — comp path (flag=false) unchanged and still visible as pending.
- [x] sync_job: family adopt (D9), external-change detection, dampening anchors reviewed.
- [ ] shopbot/ M1-M3 re-verification on today's code
- [ ] groups.py settle paths (targeted — billable_bytes reuse confirmed by grep)
- [ ] frontend build + review
- [ ] infra/security sweep
- [ ] S1 price-sheet comparison, C10-C12 checks

## Phase 2+ — fixes, global tests, docs

- [x] **MONEY-ADJ-1 (P0)** adjust double-billing — red test + live-data proof (account 39: 300,000 charged twice); fixed via `bill_added_gb` contract; commit `e1bf05b`.
- [x] **BOT-DEBT-1 (P1)** debt console double-tap = double credit — red state proven; 10s repeat-guard; commit `f9bcd65`.
- [x] **DEL-F5 (P1)** delegate charge endpoints outside the billing lock — red concurrency test (2 charges, 1 extension); `@serialise_billing` + postponed-annotations landmine removed; commit `adbc06f`. Includes the first automated lock of delegate_bot's 3369200/efe410c fixes (`delegate_bot/test_renew_guard.py`).
- [x] **C12-HEARTBEAT (P2)** notify heartbeat + summary fields; commit `41af135`.
- [x] Final convergence: backend **27/27**, bot **5/5**, delegate guard **7 cases pass**, frontend build clean, money invariant re-verified on the live copy (Σ 17,729,176.09 == buckets, 0 ownerless) AFTER all fixes.
- [x] Docs: `docs/audits/2026-09-27_round_01_five_areas.md`, `docs/audits/2026-09-27_r_catalog_verification.md` (R1–R30), `docs/proposals/2026-09-27_adjust_double_billing_remediation.md` (live-money remediation, PROPOSAL ONLY), `DECISIONS.md` D14–D17, `DOMAIN_AND_BILLING.md` adjust contract, `ci.yml` delegate-bot job.
- [ ] Deliberately NOT done (honest): full-panel Playwright U1 (no fake-Marzban harness — same disclosure as Addendum 5); model council unavailable in this environment (fallback per brief §6.10 documented); S1 pricing decision belongs to the owner; remediation SQL belongs to the owner/operator.
- Deploy (Phase 6): NOT in scope of this session — owner gate is separate. Nothing pushed; branch `fix/vpn-full-review-2026-09-27` holds 5 commits total.
