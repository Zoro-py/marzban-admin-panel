# Domain model, billing, and automation — full reference

This is the deep reference for how money and Marzban state actually move in this system.
`README.md` gets you running; this file explains what the running system actually *does* and
why, so it can be handed off or picked up cold. Read `AGENTS.md` too — it documents past
failure modes in this exact codebase and the discipline required around money logic.

## 1. Core entities

| Entity | What it is |
|---|---|
| `Account` | Mirrors one Marzban user. Carries local billing fields Marzban has no concept of: `billing_mode`, `rate_per_gb`, `usage_baseline` (payg meter start point), `billed_data_limit` (prepay meter start point), `customer_id`/`group_id` (ownership). |
| `Customer` | The person you actually deal with. Owns standalone accounts and/or represents a `Group`. |
| `Group` | A pay-as-you-go-or-prepay billing unit — several accounts billed together against one representative `Customer`, on a shared cycle. A group's `billing_mode` governs every member's billing regardless of that member's own field (see §2). |
| `LedgerEntry` | Append-only. `type` is `charge` (debt owed to you) or `credit` (payment received). Never edited or deleted — a balance is always a live sum, not a stored field. |
| `QueuedPlan` | A next-plan "pending" row — see §4.2. |
| `MonthlySettlementBatch` | One row per group/account settled by a single monthly payg run — see §4.4. |
| `AppSettings` | Singleton row (`id=1`). Holds `default_rate_per_gb` and `last_payg_monthly_settlement` (the self-healing marker, §4.4). |
| `AccountEvent` | Audit trail for direct Marzban actions on an account (reset, settle_reset, next-plan activation, etc.) — not money itself, just "what happened and when." |

### The ledger model — ONE OWNER PER ENTRY

Every `LedgerEntry` is owned by exactly one scope, resolved in this order:

```
account_id set     -> that account's own money
else group_id set  -> group-level money not tied to any one member
else customer_id   -> customer-level money not tied to any account
```

The other FK columns are still written (for filtering/history views) but **never** summed
into a balance at more than one level — an entry counted twice is exactly how one member's
payment could flip a whole group's balance. Roll-ups (group balance, customer balance) are
sums of the level below, never independent re-queries. See `app/services.py` (`MoneyBook`,
`account_posted_balance`, `group_only_posted_balance`) for the actual balance math.

## 2. Billing modes

**prepay** — pay for a package (a `data_limit`) sized and priced *up front*. Billable amount
on settle = `data_limit - billed_data_limit` (the whole package, once). Marzban itself
enforces the cap by blocking the account at `data_limit`.

**payg** — pay for what was *actually used* since the last settle. Billable amount on settle
= `used_traffic - usage_baseline` (metered). A payg account can still optionally carry a
Marzban `data_limit` as a hard technical safety cap (independent of billing) — see §4.3.

`app/services.py::billable_bytes(account, mode)` is the one function that computes this; both
settle endpoints and every automatic job call it, never reimplement it.

**Effective mode vs raw field**: a grouped account is *always* billed by its **group's**
mode, regardless of what its own `billing_mode` field says (a member added to a payg group
keeps whatever default field it was created with, but bills as payg). Use
`effective_billing_mode(session, account)`, never the raw `account.billing_mode`, anywhere a
decision depends on "is this actually payg." Same idea for rate: `effective_rate()` resolves
account's own rate → group's rate → dashboard-wide default, in that order.

## 3. Settle vs Reset — two related but distinct actions

| | **Settle** (`POST .../settle`) | **Reset** (`POST /accounts/{id}/reset`) |
|---|---|---|
| Posts a ledger charge | Always (for whatever's owed) | Only if `charge_amount` given (or auto-computed for payg) |
| Touches Marzban | **payg only** (resets `used_traffic` to 0) | Always, any mode |
| Rolls local baseline | Yes (`usage_baseline` for payg, `billed_data_limit` for prepay) | Yes, same fields |
| Typical trigger | Manual, from the dashboard, or one of the automatic jobs in §4 | Manual — "start a fresh cycle right now" |

**Why settle resets Marzban usage for payg but never for prepay**: for payg, the meter should
genuinely read 0 once you've billed for what's on it — that's what "settled" means for a
metered account. For prepay, `data_limit` is the cap on a package **already sold**; zeroing
`used_traffic` mid-package would hand the customer a second free allowance of the same
package instead of billing anything. This was a live fix (2026) — before it, `settle` only
ever moved the local `usage_baseline`, and Marzban's own usage counter for a payg account
kept climbing regardless of how many times it had been billed.

**Failure handling differs by blast radius**, and this distinction is deliberate — see
`AGENTS.md` §4.5 for the general rule (a DB rollback cannot undo an already-sent Marzban API
call):

- `settle_account` / `settle_group_member` (exactly one account): the Marzban reset is
  attempted *before* any DB write. If it fails, the whole request aborts cleanly —
  `HTTPException(502)`, nothing charged, nothing changed. Safe, because nothing happened yet.
- `settle_group` (many members at once): each member's Marzban reset is attempted
  independently. A failure on member 3 does **not** roll back or skip members 1–2 (their
  resets already happened, for real, and can't be undone) — and does not stop member 3 from
  still being charged (the amount was already computed from its pre-reset usage). Failures
  are collected into `failed_resets` in the response and surfaced to the operator as a toast
  warning. This mirrors `reset_group_cycle`'s pre-existing, deliberately-documented reasoning
  in the same file.

**`pay_scope`** (`AccountSettleRequest`/`GroupSettleRequest`, default `"full"`): when
`mark_paid=True`, `"full"` credits whatever's owed *after* this cycle's charge lands (old
debt + new charge, the common case). `"prior_only"` credits only the balance that existed
*before* this charge — for "the old cycle just got paid, but this new one hasn't been yet,"
so today's payment doesn't get silently applied to a charge nobody's actually paid for. Shown
in the dashboard only when there's an actual prior balance to separate out.

## 4. What happens automatically (no button click)

All four of these run out of the same 60-second sync cycle (`app/sync_job.py::_run_sync_impl`,
guarded by an `asyncio.Lock` so a manually-triggered `/api/sync/run` can never overlap the
scheduled one) except §4.4, which is a separate daily cron check.

### 4.1 The "notify-first" safety pattern

Two deliberate modes, used consistently across every automatic action below:

- **Notify-first** (block the action on the notification succeeding): used wherever an
  algorithm is making an **unreviewed** decision — queuing a next plan, the payg
  monthly-settlement aggregate report, a payg cap-hit charge+reset. If Telegram can't be
  reached, *nothing happens* — no charge, no Marzban call — because an operator who never
  sees the notification never gets the chance to review or reprice what the algorithm picked,
  and the effect would otherwise be indistinguishable from silently handing out free data or
  money nobody agreed to.
- **Notify-after, best-effort** (write first, swallow notify failures): used only where the
  action was **already approved** earlier — activating a next plan, since the operator
  approved the queued amount when it was created (by hand, or by seeing the auto-queue
  notification). A failed *success* notification here is logged but never blocks or reverts
  the activation that already happened.

`app/notify.py::notify_admin()` raises on failure (never best-effort itself) — callers decide
which of the two modes above applies to their situation.

### 4.2 Next-plan auto-queue + auto-activation (prepay only)

When a **prepay** account (via `effective_billing_mode`) drops to about
`NEAR_QUOTA_AUTO_QUEUE_REMAINING_GB` of its package **or** within
`NEAR_EXPIRY_AUTO_QUEUE_REMAINING_DAYS` (1 day) of expiry, and has no `QueuedPlan` already
pending, a plan is auto-computed from `monthly_avg_usage()` and queued:

- Package size is rounded to a clean multiple of 5 GB, rounding **down** by default (never
  queue more than what was actually observed) — **except** when the average is within 1 GB
  of the *next* multiple of 5, in which case it rounds **up** (19.8 GB → 20, not 15 — a
  reported real-world case where rounding down cut the queued plan by nearly 5 GB over a
  rounding technicality). See `_round_package_size`.
- Duration defaults to `AUTO_NEXT_PLAN_DURATION_DAYS` (31 days).
- **Notify-first**: the admin gets an aggregate message plus a ready-to-forward customer
  message (usage stats embedded as text, not a screenshot) *before* the `QueuedPlan` row is
  written. A failed send means nothing gets queued — the next sync cycle (60s later) retries.
- Never triggers for `disabled` or `deleted_from_marzban` accounts (`_EXCLUDED_STATUSES`,
  shared with every other automatic action in this file) — an operator turned it off on
  purpose, or there's nothing left to renew.
- **Standing decision (do not silently re-introduce)**: there is deliberately **no review
  delay** between auto-queue and auto-activation — the operator explicitly chose to keep this
  zero-delay after a real incident (five accounts auto-activated within ~1 minute of being
  queued, totaling 950,000 Toman, some priced off near-zero usage averages for churned
  customers). The fix that shipped instead was the rounding fix above plus activation
  success/failure notifications (below) — not a review window. Existing charges from that
  incident were deliberately left untouched (operator reviewed them by hand).

When Marzban later reports the account as `limited`/`expired`, the pending plan
auto-activates: the old plan is auto-settled (unbilled amount charged), the new
data_limit/expire/reset applied via Marzban, and:
- **Success** → best-effort admin notification (old-plan charge amount + new plan specs).
- **Failure** → admin is notified of the error; the plan stays `pending` and retries next
  cycle (or can be fixed by hand). This failure-visibility was itself a fix — previously a
  failed activation was only logged server-side, invisible to the operator, which is why an
  overdue-looking account could sit unrenewed with zero explanation.

### 4.3 Payg cap-hit reactive settlement

A payg account can carry a Marzban `data_limit` as a hard technical cap even though it's
billed on metered usage. If it's hit (Marzban blocks the account), `_maybe_settle_payg_cap_hit`
fires on the very next sync cycle: computes the accrued charge, **notifies first**, then calls
Marzban's `reset_user` (unblocking it immediately) and posts the charge (only if `amount > 0`).
Purely reactive — threshold is `0.0` remaining GB, no lead-in, because there's no "renewal" to
prepare ahead of time for a metered account, only "they're capped right now, bill it, free
them."

### 4.4 Monthly payg settlement (real Jalali calendar month-end)

On the night of the last day of each real Persian (Jalali) calendar month —
`payg_monthly_settle_hour`/`minute`, default 23:30 server time — every payg group and
standalone payg account gets its accrued usage charged and Marzban-reset, exactly like
clicking "Settle" would (it *is* `settle_group`/`settle_account`, called directly).

- **Self-healing, not "did today run"**: `AppSettings.last_payg_monthly_settlement` tracks
  the last *completed* period ("1405-05"). `_target_settlement_period()` computes fresh, every
  day, "the most recent period whose last day has been reached but isn't marked done yet" —
  so a single failed night (Telegram down, a DB error) retries automatically on every
  subsequent day instead of silently skipping the rest of the month. This is why the check
  runs **daily**, not "only on the actual last day."
- **Notify-first at the aggregate level**: the whole batch's summary (every group/account,
  GB, amount, grand total) must send successfully before *anything* is charged or reset. A
  failed send blocks the entire night's run (retried the next day per above).
- **Per-entity failure isolation** within the batch: one group/account raising an exception
  doesn't stop the rest from settling — it's logged, reported in a follow-up admin message,
  and naturally gets picked up again next month's cycle (unbilled usage just keeps accruing).
- Each settled group/account gets its own `MonthlySettlementBatch` row (`jalali_period`,
  `billable_gb`, `amount`, `settled_at`) plus its own ready-to-forward customer message.
- **The web panel's "Monthly Settlements" page** (`/monthly-settlements`) is where "mark this
  period as paid" happens — deliberately decoupled from settle time, so a day or two of
  payment lag doesn't corrupt ongoing balance math. "Mark as paid" (`POST
  /api/payg-monthly/batches/{id}/mark-paid`) credits **exactly that batch's amount**, not the
  entity's whole current balance (which could include unrelated debt). This was an explicit
  design choice: the operator wanted this as a web-panel feature, not a bot command.
- `POST /api/payg-monthly/run` lets an operator trigger the same check manually (e.g. to
  verify the whole pipeline works) — most days it's a no-op (nothing unsettled yet).

## 5. Dashboard-specific live widgets

- **Online accounts chart** (Dashboard): trend of currently-connected accounts, sourced from
  `OnlineSnapshot` rows written as a side effect of the regular sync cycle (granularity =
  `SYNC_INTERVAL_SECONDS`, not real-time). Includes a computed **"quietest window"**
  insight — buckets every loaded point by local hour-of-day, averages online-count per hour,
  and reports the 3-hour circular window with the lowest average (a recurring daily lull, not
  just one day's dip) — built specifically to help pick a low-impact maintenance window.
  Hidden when fewer than 6 distinct hours have been observed (not enough data to trust a
  pattern).
- **Server resources chart** (Dashboard): live host CPU%/RAM%, polled every 3s from
  `GET /api/reports/system-status` (uses `psutil`; `load_avg_1m` is POSIX-only, `null` on
  Windows dev rather than a fake 0). Deliberately **not persisted** — a 4-minute rolling
  buffer kept client-side, since this is a "watch it react right now" tool, not a trend
  archive. Polling pauses automatically when the browser tab isn't focused.

## 6. Known infrastructure note (not code, but relevant to anyone running this)

On the current production server, a `top` reading with high `%st` (CPU **steal** time — the
hypervisor withholding CPU this VM was scheduled to get) was traced to the HostVDS
**Burstable** plan tier throttling the box under Marzban's own real load, not to anything in
this codebase. Confirmed by identifying the actual PID via `/proc/<pid>/cwd` →
`docker ps --filter id=...` → it was Marzban core (`gozargah/marzban:latest`), not this
dashboard's own backend. Fixed by moving to HostVDS's **Highload** tier (dedicated CPU) — same
RAM-per-dollar tier as one step up in Burstable, but with a real CPU guarantee instead of a
shared/oversold one. If this ever recurs, check `%st` in `top` first before suspecting this
app's own code.
