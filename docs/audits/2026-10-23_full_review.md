# Full pre-public review — 2026-10-23 (fresh money surface + UX/state coverage + live walkthrough)

Repo at `2687218` (= server HEAD, deployed). Method: two independent read-only
sweeps (money/state machine + UX/journey coverage), every load-bearing claim
re-verified by hand against the files, plus a LIVE browser walkthrough of both
surfaces (customer subscription page + operator panel, desktop 1440 + mobile
390) and live endpoint probes from outside the server. Nothing was edited.

Context: the 2026-10-20 prelaunch query (claims 1-9) is confirmed fixed in
`a9cde83`+`a0a751b` — spot-verified: `roll_usage_baseline_on_payg_entry`
(services.py:555), shop-extend `billed_data_limit` bump (:1550), delegate payg
refusal (delegate_service.py:187), debt console `:.2f` callback amounts
(debt.py:315,361), `/charge`//credit idempotency keys (customer.py:121).

## Verified live today (all green)

- Server runs `2687218`; all five containers up.
- Customer sub page IS the Marzban template at
  `/var/lib/marzban/templates/subscription/index.html`, served at
  `https://v1.melobuds.ir:8443/sub/<token>` (HTML for `Accept: text/html`,
  raw base64 config otherwise). `MARZBAN_SUBSCRIPTION_BASE_URL` is unset, so
  the panel's `resolve_subscription_url` falls back to
  `MARZBAN_BASE_URL=https://v1.melobuds.ir:8443` — the shopbot link button
  works.
- The sub-stats chain works end-to-end through Cloudflare:
  `ota.gibojet.ir/sub-stats/<token>` → nginx (`zoro-ota.conf`,
  `/sub-stats/` → `127.0.0.1:8010/api/sub-stats/`) → 200 with hourly/daily
  points, for both an old and a freshly rotated token. Token signing matches
  Marzban (the "total 404 lockout" worry is refuted live).
- Operator panel behind CF: `ops.melobuds.ir` → 8011, `ops-api.melobuds.ir`
  → 8010. Login, dashboard money tiles, notify-heartbeat line (healthy),
  upcoming-renewals, accounts board, shop settings all render; mobile 390px
  is clean (hamburger, stacked tiles, filter chips, Jalali/Gregorian toggle).
- `BOT_RECEIPTS_THREAD_ID` not set in live env → receipt topic routing is
  latent (findings below apply only once it is set).
- Retail PAYG is currently OFF in production (settings checkbox unchecked,
  rate disabled). Every retail-PAYG finding below activates when it is
  switched on.

## P0

1. **Stuck-order sweep resolves money in silence (both directions).**
   `shop_service.sweep_stuck_orders` (:544-599): the late-delivery branch
   calls `_record_delivered` but never `deliver_order_to_customer` (the
   approve path does, routers/shop.py:336) — the customer paid and never
   gets QR/link/renewal message. The refund branch posts
   `refund_order` with no customer message at all. Fix: deliver/notify from
   the sweep, mirroring routers/shop.py:336.
2. **Retail-PAYG welcome caption says «سرویس تست … رایگان».**
   Payg orders are `price=0` (:1006,1142,1894); `deliver_order_to_customer`
   sets `is_trial = order.price == 0` (:886) →
   `shop_texts.delivery_caption(is_trial=True)` renders the free-trial copy
   for a metered, wallet-billed service. Fix: explicit order kind / payg
   caption.

## P1

3. **Normal buy CAPS a retail-payg "unlimited" service.**
   Payg accounts are created with Marzban `data_limit: 0` (:1907, comment
   "unlimited — the wallet…"), but the unlimited guards read `is None`
   (:1466-1470, :1580-1590). A MENU_BUY package on a payg account passes
   every guard, sets `data_limit = 0 + N GB` (total cap, no_reset), the
   customer pays the package AND the daily meter keeps charging per GB, and
   once capped, `_maybe_settle_payg_cap_hit` (sync_job.py:350-356, which
   only skips falsy `data_limit`) bills the residual day on the RESELLER
   ledger (`customer_id=NULL`, uninvoiceable) at the dashboard default rate
   (services.py:639-647), not `payg_rate_per_gb`. Fix: treat falsy 0 as
   unlimited in the shop guards, and/or refuse payg-mode renewable accounts
   in `purchase`; skip `customer_id IS NULL` in the cap-hit handler.
4. **Clearing `payg_rate_per_gb` silently stops all metering.**
   `rate = account.rate_per_gb or settings.payg_rate_per_gb; amount = … if
   rate else 0` (:1988-1990); the enable-time validation applies only when
   `payg_enabled` is truthy in the same PATCH. Zero rate = free service with
   suspension math degenerating to 0. Fix: refuse zeroing while payg
   accounts exist.
5. **Copy-link + all app deep-links put a HOST-LESS URL on the clipboard.**
   The Marzban template renders `{{ user.subscription_url }}` relative
   (`/sub/<token>`; prefix unset in Marzban .env); the copy button copies
   the input value verbatim (template :2477-2479) and
   `updateClientDeepLinks` (:2038-2050) encodes the same relative path into
   v2rayNG/Hiddify/Streisand/V2Box schemes. The page's own FAQ ("Import
   from Clipboard") fails at this step. Fix: set
   `XRAY_SUBSCRIPTION_URL_PREFIX=https://v1.melobuds.ir:8443` in Marzban
   .env, or absolutize with `location.origin` in the template.
6. **Shortfall < min_topup is an unfinishable loop.** The bot asks the
   customer to transfer the exact shortfall (shop.py:599-605) but
   `create_topup` refuses anything < 10,000 T (:646-649) → non-409 →
   «رسیدتان ثبت نشد — دوباره بفرستید» forever. Fix: order-bound shortfall
   exemption or a different instruction.
7. **«سرویس‌های من» renders suspended/disabled/debt-paused services like
   working ones.** `ShopBotAccountRow.status` is sent (routers/shop.py
   :1351-1362) but `show_accounts` + `texts.account_line(volume, used,
   days)` (:419) never read it. Same for a payg-suspended row (renders as
   normal unlimited with a working-looking link button). Contradicts the
   project's own state matrix (2026-10-01 proposal §2).
8. **Failed instant wallet purchase: told «کاری انجام نشد» while money was
   taken and refunded.** routers/shop.py:905-906 raises 502 after the
   automatic refund; bot maps every ShopApiError to generic_error
   (shop.py:623-626). Fix: map 502/failed to a refund-stated message.
9. **`/extend` has neither confirm nor idempotency** (bot/handlers/
   account.py:72-98) — the CLAIM-7 standard reached `/charge`//credit` but
   not `/extend`; a retry double-charges and double-extends.
10. **Dashboard wallet adjustment fails silently** — the `adjust` mutation
    has no `onError` (ShopPage.tsx:311-319); same on settings save
    (:515-544).
11. **Delegates get zero lifecycle notifications** — no job touches
    delegate accounts (main.py:159-202); expiry/discovery is the family
    finding out the VPN stopped.

## P2

- Daily payg meter double-commit: wallet entry commits (:1993-1996) before
  the baseline commit (:1997-2001) — crash between = same GB billed twice
  tomorrow. Make it one transaction.
- "Reactivates the moment the wallet clears" is actually next daily tick
  (00:45), up to ~24h late, with a suspend/top-up await race
  (:2003-2022); customer copy promises immediate (shop_texts.py:372-378).
  Fix: reactivate from the 15-min debt sweep / topup hook.
- Failed Marzban disable → unbounded negative wallet, operator never told
  (:2009-2011).
- Suspended payg customer can start a SECOND payg service (gate uses
  `renewable_account`, which skips disabled — :1395-1403, :1885-1887);
  top-up later reactivates BOTH meters onto one wallet.
- `payg_enabled=false` doesn't stop metering live payg accounts (sweep
  never checks the toggle, :1964-1974) — ambiguous with the UI label.
- Customer never told about manual wallet adjustments; operator never told
  when a wallet goes negative / payg suspends; typed receipts never get the
  late-apology (filter `receipt_file_id.is_not(None)`, :2084).
- Chart on the sub page: any fetch failure hides the whole card silently
  (`historyLoadRange` `.catch → historyHideCard()`, template :2149-2159) —
  observed live once (transient); flaky mobile networks will lose the
  feature with no message and no retry. Empty-data case is handled well.
- 8010/8011 bind `0.0.0.0` with no ufw — reachable from the open internet
  bypassing Cloudflare (auth still applies). Bots use the docker network
  (`http://backend:8000`) and nginx uses 127.0.0.1, so `BIND_ADDRESS=127.0.0.1`
  for both breaks nothing.
- Multi-service customers can't choose what a purchase extends (newest-live
  silently); no per-service «شارژ همین سرویس» button (open proposal §3).
- Delivered-untracked orders render as phantom «سرویس نامحدود» with no link
  button (routers/shop.py:1341-1362).
- `/charge`//credit` still have no confirm step (idempotency fixed).
- delegate_bot prints the raw sub URL inline (bidi hazard shopbot fixed
  with a button); shopbot burst-send lacks delegate_bot's RetryAfter guard.
- Payg meter debits show in wallet history as «خرید سرویس» (type=purchase,
  :1993-1995) — a meter charge reads as buying a plan.

## P3

- sub_stats `!=` instead of `hmac.compare_digest`; missing Marzban tables
  → raw 500 instead of 503; unauthenticated per-request sqlite connects
  (same class as Marzban's own sub endpoints).
- Receipt thread routing splits streams once `BOT_RECEIPTS_THREAD_ID` is
  set (photos threaded, text/fallback General) and the late-approval alert
  has no fallback if the topic id breaks (shop_service.py:2106-2111).
- Double-tap auto-renew with no server cooldown when the first renewal
  stayed ≥ 0 (only `debt_since` guards).
- Payg start order id stamped on meter wallet entries (:1995); shop list
  ordering lacks an id tiebreak (routers/shop.py:1245); warning line is
  `rate×2` while the constant says "LAST charged amount" (:1865/:2042);
  payg list row shows «~۳۶۵۰ روز».
- Dead copy drift: `delivered_but_no_qr`, `payment_request` (backend vs
  bot), `ASK_RECEIPT`/`BUY_BUTTON`/`trial_offer` unused; on_error/wallet
  error paths reply without the main keyboard; blocked customer tapping
  trial gets generic_error; no terms/consent step in the shop journey
  (worth an explicit decision).
- Operator panel: Playwright-driven clicks fail actionability on the login
  button and nav links (needed JS-dispatched pointer sequences + Enter) —
  needs one manual check in a real browser (may be automation-specific).
- Sub page: mixed Latin/Persian numerals and a raw Gregorian expiry line;
  «کپی شد» toast present without user action on first load.

## Explicitly verified CLEAN

- Double-billing matrix for retail payg: payg_monthly_job skips
  customer_id-NULL accounts (:144-152); manual settle refuses unassigned
  (:948-952); delegate renew can't reach them (:74); prepay paths
  prepay-gated (:220).
- Sweep↔sync and sweep↔purchase concurrency on the single event loop; the
  missed-cron case self-heals (baseline advances only on successful
  charge).
- Bounded-debt auto-renew: bound enforced pre-debit via `debt_since`
  (:1717-1737), refunds only on panel-verified non-landing
  (:1510-1540), no orphan debt after a failed extend, one reminder per
  episode, warnings once per order per type, `auto_renewed` orders
  excluded from the next average.
- Receipt announcements cannot be silenced by topic misroute (3-level
  fallback, routers/shop.py:1214-1233).
- sub_stats: parameterized SQL, ro URI, range whitelist, uniform 404s, no
  secret leakage; token signing matches deployed Marzban (live-proven).
- Create-new-service: one Marzban user, one debit, provisional guard
  semantics preserved; dedupe (396f9c1) drops nothing.
- Shop settings UI honest copy (approval-promise, bridge, trial), payg
  enable gate can't save without price/card; mobile panel layouts clean.

---

## Addendum (same day, evening) — the fix package

Every P0/P1 above landed on branch `fix/prepublic-p0p1-2026-10-23` (NOT deployed — deploy
sheet: `docs/deploys/2026-10-23.md`; decision write-up: `docs/DECISIONS.md` D36). Fix designs
were reconciled through a 7-voice council (2 Seyed lenses included); F3b became the standing
product rule «PAYG بسته نمی‌خرد» and F10 was Seyed-adjusted to never ask for a sub-minimum
transfer. The review pass over the implementation caught one REAL production bug the tests
had introduced alongside their fix: the F7 reactivation call commits mid-endpoint, which
expires the SQLModel identity-map row and made `ShopTopupRead.model_dump()` return {} — the
approve endpoint 500'd AFTER crediting the wallet. Fixed with `session.refresh(topup)` before
serialization; the trap is documented in D36 (commit = expire; refresh before re-serializing).

Test tally at package freeze: backend 38 green + the 4 pre-existing baseline reds
(test_merge_family/test_receipt_alert/test_receipt_text_dedupe/test_statement — red on main
too, out of scope); bot 5 green + 2 pre-existing reds (test_debt_console/test_since);
shopbot 2/2 (incl. the new 26-check script); delegate_bot 3/3; `tsc --noEmit` clean.

Not fixed here, deliberately: P2s beyond the listed ride-alongs, the receipt-limbo product
work, `on_error` keyboard, and the Marzban-side items (env prefix + template) which deploy
separately per the sheet.
