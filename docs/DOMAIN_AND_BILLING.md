# Domain model, billing, and automation — full reference

This is the deep reference for how money and Marzban state actually move in this system.
`README.md` gets you running; this file explains what the running system actually *does* and
why, so it can be handed off or picked up cold. Read `AGENTS.md` too — it documents past
failure modes in this exact codebase and the discipline required around money logic.

## 1. Core entities

| Entity | What it is |
|---|---|
| `Account` | Mirrors one Marzban user. Carries local billing fields Marzban has no concept of: `billing_mode`, `rate_per_gb`, `usage_baseline` (payg meter start point), `billed_data_limit` (prepay meter start point), `customer_id`/`group_id` (ownership). |
| `Customer` | The person you actually deal with. Owns standalone accounts and/or represents a `Group`. Carries a label-only `kind`: `individual` (default) or `family` — one payer owning several accounts (see "Families" below). |
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
  purpose, or there's nothing left to renew. Same for any account with
  `Account.auto_renew_enabled = False` — an explicit per-account (or whole bulk-created batch,
  set once at creation) opt-out for accounts the operator always wants to renew by hand
  (comp/staff/family accounts). Default `True`; existing accounts were unaffected when this
  field was introduced.
- **Standing decision (do not silently re-introduce)**: there is deliberately **no review
  delay** between auto-queue and auto-activation — the operator explicitly chose to keep this
  zero-delay after a real incident (five accounts auto-activated within ~1 minute of being
  queued, totaling 950,000 Toman, some priced off near-zero usage averages for churned
  customers). The fix that shipped instead was the rounding fix above plus activation
  success/failure notifications (below) — not a review window. Existing charges from that
  incident were deliberately left untouched (operator reviewed them by hand). The dashboard's
  **Upcoming renewals** card (`GET /api/reports/upcoming-renewals`) is the visibility half of
  that same tradeoff: every pending `QueuedPlan` with its estimated charge, so it can be
  checked between being queued and actually activating instead of only found out about
  afterward.

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
  `billable_gb`, `amount`, `settled_at`) plus its own ready-to-forward customer message. The
  batch row's `amount` is what the settle **actually posted** (recomputed at settle time),
  never the figure computed minutes earlier in the aggregate phase — "mark as paid" credits
  exactly this row, so the two must agree to the toman. Ledger rows and account events from
  the run carry `created_by="system:payg-monthly"` (the settle endpoints refuse to run with
  FastAPI's unresolved `Depends()` default — direct callers must name themselves). A member
  whose Marzban reset failed inside a group settle is still charged (its local baseline rolls
  forward from the pre-reset meter), does NOT count as a failed settle, and gets its own
  warning message — the meter was never zeroed and somebody has to know.
- **The web panel's "Monthly Settlements" page** (`/monthly-settlements`) is where "mark this
  period as paid" happens — deliberately decoupled from settle time, so a day or two of
  payment lag doesn't corrupt ongoing balance math. "Mark as paid" (`POST
  /api/payg-monthly/batches/{id}/mark-paid`) credits **exactly that batch's amount**, not the
  entity's whole current balance (which could include unrelated debt). This was an explicit
  design choice: the operator wanted this as a web-panel feature, not a bot command.
- `POST /api/payg-monthly/run` lets an operator trigger the same check manually (e.g. to
  verify the whole pipeline works) — most days it's a no-op (nothing unsettled yet).

### 4.5 Every-other-day overdue-debt nudge (informational only, not on the sync cycle)

`app/debt_nudge_job.py`, on its own cron (`debt_nudge_hour`/`minute`, default 09:00 server
time) — not part of the 60-second sync cycle, since it reads already-posted ledger history
rather than anything from Marzban. The cron fires daily and the job itself skips every other
calendar day (`toordinal()` parity — a plain "every 2 days" cron drifts at month boundaries,
and a stateless parity check never needs "did it run yesterday" tracking). Sends one Telegram
summary of every customer whose **posted** (real, already-charged — never the pending/unbilled
estimate) debt has been continuously outstanding for at least `DEBT_NUDGE_MIN_DAYS` (14).

Explicitly a **duration** gate, not an amount one, per the operator's own framing: a customer
charged five minutes ago isn't overdue in any useful sense; one who's owed the same amount for
six weeks is. Debt age is computed by replaying that customer's own ledger scope (their
accounts, every group they represent, entries posted directly to them) in date order and
finding when the running balance most recently crossed from zero-or-below into positive and
*stayed* there — a customer paid off a month ago and charged again yesterday has 1-day-old
debt, not 30-day-old, because every crossing back to positive resets the start point. A
group's debt is always attributed to its **representative customer** here, never the group
itself (a group carries no wallet of its own to be nudged about).

Purely informational — no charge, no Marzban call — so unlike the money-moving jobs above it
has no self-healing "did this week already run" state: a failed send is logged and the next
week's scheduled run simply tries again. Sends nothing most weeks by design (no customer
matches); that's the "without creating noise" requirement working as intended, not a bug.

**The «accruing» list (read-only, same endpoint).** `GET /api/notifications/debt-nudge` also
returns `accruing`: customers whose net owed (posted + not-yet-invoiced) is positive but who are
NOT in `overdue` — the debt is younger than 14 days, or was never billed (a prepay package nobody
has charged). It is never nudged and has no payment buttons; the bot's `/debts` shows it as a
quieter second section so «not overdue» can't be misread as «owes nothing». The nudge itself is
unchanged: posted debt only, ≥14 days (owner decision).

### Families (`Customer.kind = 'family'`)

A family is **one customer who owns several accounts** — the operator talks to that one payer. It
is a label, not a billing mode: a family's balance is the same account roll-up every customer
gets, and no money code reads `kind`. What it changes is where things land:

- `POST /api/accounts/bulk` with neither `customer_id` nor `group_id` attaches the whole batch to
  ONE customer named after the base name — an existing same-named customer (case-insensitive) is
  reused, otherwise a new `kind='family'` one is created in the transaction of the first account
  that succeeds (a batch where every item fails leaves no empty customer). `unassigned=true` is
  the explicit opt-out (test accounts). The bot's `/bulk` and the panel's Bulk dialog both go
  through this rule.
- Sync adopts an unknown Marzban user named `<base><number>` into an existing **family** customer
  named `<base>`; anything else still gets its own personal customer (the standing «every account
  is an individual by default» rule). Only families are matched, so an ordinary customer sharing
  a name prefix is never swallowed.
- Why it exists: before this, a batch made without an owner became N one-account customers
  (live: `khanevadeh1..12`) — twelve rows in every debt list instead of one payer.
- Existing fragmented families are folded with `scripts/merge_family_customers.py` (dry-run by
  default; see `docs/runbooks/merge-family-customers.md`). It moves accounts and re-points ledger
  `customer_id`; it never creates or edits a ledger amount and aborts if the ledger total, the
  family's posted balance or the account count would change.

## 5. Dashboard-specific live widgets

- **Upcoming renewals card** (Dashboard): every pending `QueuedPlan` with its estimated charge
  and days until activation — see §4.2's closing note. Read-only, changes nothing; exists so
  a queued auto-renewal can be checked before it activates, not just after.
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

---

## 7. The self-serve shop — a second, separate money system

Added alongside the reseller side, not inside it. Everything in sections 1-6
above describes money the operator is **owed**; this section describes money
the operator has **already been paid** and holds on a customer's behalf. The
two are different quantities and are never mixed.

### 7.1 Why it is not in `LedgerEntry`

`LedgerEntry` is summed by `services.py` into account → group → customer
roll-ups, and every one of those assumes each row is debt accrued against a
reseller customer. A prepaid wallet balance in that table would be added into
those figures, making every customer balance on the dashboard wrong — the same
shape as the failure in AGENTS.md §4.3 where a CHARGE posted to cancel a
CREDIT erased money a customer was legitimately owed.

So the shop has its own append-only ledger, `ShopWalletEntry`, summed by
`shop_service.wallet_balance` and read by nothing in `services.py`. A
`ShopUser` may be linked to a `Customer` for reporting; that link carries no
money in either direction.

### 7.2 Integers, not floats

Wallet amounts are whole Toman stored as `int`. The reseller ledger uses
`float` for historical reasons, which is survivable there because a settle
computes an amount once from usage. A wallet is different: it is repeatedly
added to and subtracted from, and float drift eventually shows a customer a
balance that disagrees with the sum of the transactions they can see.
Toman has no subunit in practice, so integers cost nothing.

### 7.3 Order of operations in a purchase — and why

`shop_service.purchase`:

1. Take a per-user lock, so two taps on "buy" serialise.
2. In ONE transaction: re-read the balance, check affordability, write the
   `ShopOrder` and its matching negative wallet entry, commit.
3. Only then call Marzban.
4. On success, write the `Account` row and mark the order delivered. On
   failure, post a compensating refund and mark it failed.

The debit happens **before** provisioning deliberately. Provisioning first and
charging after loses real inventory on any crash in between — a live account
nobody paid for, indistinguishable from a legitimate one. This ordering's
worst case is an order stuck in `provisioning` with the money held, which
`sweep_stuck_orders` (every 5 minutes, threshold 10 minutes) resolves.

**Nothing is refunded without asking the panel first.** A failed or
timed-out Marzban call is not evidence that nothing happened: a read timeout
loses the *response*, not necessarily the work. Refunding on that signal alone
was measured to produce a full refund plus a live, unbilled account. So both
the provisioning path and the sweeper check the panel before refunding:

- for a **create**, "does the user exist with the `note` we stamped on it"
  (`_find_our_marzban_user`) — the note stops an operator's hand-made user of
  the same name from being adopted;
- for a **renewal**, "have the account's limit and expiry already reached the
  targets recorded on the order before the call" (`_extension_landed`).

Found → deliver and keep the charge. Not found, or the lookup itself failed →
refund. That is the safe direction to be wrong in: a refunded customer whose
service does exist is recoverable by the operator; a charge for nothing is not.
`_record_delivered` / `_record_extended` also refuse to write `delivered` over
an order the sweeper already settled in its own session — they re-charge
instead, allowing a negative wallet (visible, one adjustment to fix) rather
than a free account (invisible).

### 7.3b Order first; renewal in place; the trial

- **Order first.** Choosing a volume creates a `ShopOrder` in
  `awaiting_payment`, which takes no money. A card payment sent for it carries
  `ShopTopup.order_id`; `approve_topup` credits the wallet, commits, and THEN
  pays and provisions that order — so one operator approval both banks the
  money and delivers the plan. Approval is exactly-once because only the call
  that moves the top-up out of `pending` reaches the delivery step. Approving
  less than the price banks it and delivers nothing; the customer is told the
  remainder and the card. The previous shape (fund a wallet, wait, come back,
  buy) lost the customers who believed the receipt was the purchase.
- **Renewal in place.** A paid order for a customer who already has a live
  account EXTENDS that account (`_extend_order`) instead of creating another:
  same Marzban user, same subscription link. Volume and days are **stacked**
  onto what remains, never reset — remaining data and days were already paid
  for. The target `data_limit`/`expire` are committed on the order before
  `modify_user` is called; they are the evidence §7.3 checks. A per-account
  lock serialises read-modify-write so two renewals cannot overwrite each
  other (same single-process caveat as the purchase lock). An account gone
  from the panel, or disabled by the operator, is not extended; a new one is
  created.
- **The trial** is a `ShopOrder` with `price = 0`: once per `ShopUser`, only
  for someone with no delivered order, off unless the operator enables it.
  `trial_taken_at` is committed **before** provisioning, so a retry loop cannot
  mint accounts — a failed trial costs the customer their trial, which is the
  safe direction for the one path that gives something away. Its expiry is set
  from `trial_hours` exactly; a paid purchase afterwards upgrades the trial
  account in place.
- **Warnings and promises** (`main._scheduled_shop_renewal_warnings`, every 15
  minutes): expiry 3 days ahead, 80% data, trial 2 hours ahead — once each per
  account (the newest order speaks for it), recorded only when actually sent.
  A receipt that waits past `approval_eta_minutes` is announced to the
  customer as late, once, and the operator is alerted.

Re-reading the balance **inside** the lock, rather than trusting a figure read
earlier in the request, is what stops a stale number authorising a purchase
the wallet can no longer cover.

**The lock is in-process.** It is sufficient only because the backend runs as
a single uvicorn process with no `--workers`. Deploying with multiple workers
or replicas silently removes this protection; the balance check would have to
move into the database (`SELECT ... FOR UPDATE` on Postgres, `BEGIN IMMEDIATE`
on SQLite). Nothing fails loudly if that happens, which is why it is written
down here as well as in the code.

### 7.4 Refunds are idempotent, approvals are single-use

`refund_order` checks for an existing refund entry against the same order
before posting one. It is reachable from both the provisioning path and the
sweeper, which can race after a restart — and because a balance IS the sum of
its entries, a double refund would invent money with no discrepancy anywhere
to notice it by.

`approve_topup` refuses any top-up that is not still `pending`. That guard is
what makes a double-tap on the operator's approve button credit once. A
rejection credits nothing and is equally single-use.

`claimed_amount` (what the customer said they paid) and `approved_amount`
(what was actually credited) are separate columns, so "claimed 500,000,
credited 50,000" stays on the record instead of the claim being overwritten.

### 7.5 The two bots have different privileges

| | `bot/` | `shopbot/` |
|---|---|---|
| Audience | the operator only, gated to one chat id | the public |
| Backend credential | Marzban admin username/password | `SHOP_BOT_API_KEY` |
| Can reach | every endpoint | `/api/shop/bot/*` only |

`SHOP_BOT_API_KEY` unset means every `/api/shop/bot/*` request is refused —
fail closed. A shop bot that cannot authenticate must not fall back to
working.

The shop bot is trusted to report *which* Telegram user is talking to it, in
the same way it is trusted to report what they asked for. That trust is
bounded: a compromised shop bot could spend its own customers' wallets, but it
cannot create money (only an operator approval does that) and cannot reach
anything outside `/api/shop`.

### 7.6 What the shop deliberately does NOT do

- It never posts to `LedgerEntry`, so shop revenue does not appear in the
  reseller-side Finance figures. Those two numbers answer different questions
  and merging them would make both misleading.
- It does not auto-approve payments. Every credit is an operator decision.
- It does not store receipt images. Only Telegram's `file_id` is kept, so a
  stolen database does not carry customers' bank receipts with it.
