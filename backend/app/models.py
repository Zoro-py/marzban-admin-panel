from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AccountRole(str, Enum):
    primary = "primary"
    sub = "sub"


class BillingMode(str, Enum):
    prepay = "prepay"  # charged manually up front when a package is sold (default)
    payg = "payg"  # pay-as-you-go — a reset/settle bills actual usage since baseline


class LedgerType(str, Enum):
    charge = "charge"   # customer/group owes us money (بدهی)
    credit = "credit"   # payment received / credit balance (طلب)


class LedgerSource(str, Enum):
    web = "web"
    bot = "bot"
    sync = "sync"
    # A charge posted by a Delegate's own self-service action (create/renew),
    # not by the operator — kept distinct from `bot` (the operator's own bot)
    # so the audit trail can tell "I charged this" from "they triggered a
    # charge themselves" at a glance. See Delegate below.
    delegate = "delegate"


class Customer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    contact: Optional[str] = None  # telegram handle / phone
    is_group_rep: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class Group(SQLModel, table=True):
    """A billing group (e.g. a company) — one unit across all member accounts.
    billing_mode decides HOW it's billed: payg computes a charge from actual
    metered usage at settle time (the group's original/default design); prepay
    means the group is billed manually (a package sold up front) via ledger
    entries instead of the usage-based settle flow — settle_group still works
    either way, but the UI treats prepay groups' pending/current-usage figures
    as informational rather than "here's what to charge"."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    representative_customer_id: int = Field(foreign_key="customer.id", index=True)
    billing_cycle_days: int = 30
    rate_per_gb: Optional[float] = None
    billing_mode: BillingMode = BillingMode.payg
    last_settled_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)


class Account(SQLModel, table=True):
    """One Marzban user, mirrored locally with ownership + a synced usage snapshot."""

    id: Optional[int] = Field(default=None, primary_key=True)
    marzban_username: str = Field(unique=True, index=True)

    customer_id: Optional[int] = Field(default=None, foreign_key="customer.id", index=True)
    group_id: Optional[int] = Field(default=None, foreign_key="group.id", index=True)
    role: AccountRole = AccountRole.primary
    rate_per_gb: Optional[float] = None  # per-account rate; overrides the group's rate when both are set
    billing_mode: BillingMode = BillingMode.prepay

    # Snapshot of Marzban state, refreshed by the sync job (source of truth is Marzban itself)
    used_traffic: int = 0
    lifetime_used_traffic: int = 0
    data_limit: Optional[int] = None
    expire: Optional[int] = None  # unix timestamp, mirrors Marzban's `expire`
    status: Optional[str] = None
    last_synced_at: Optional[datetime] = None

    # `used_traffic` value as of the last pay-as-you-go settlement (individual
    # or as part of a group). Billable usage for the current cycle = used_traffic
    # - baseline. Matches Marzban's own "current usage" figure directly — what
    # the operator sees in Marzban is what gets billed, no separate "lifetime"
    # concept the operator never asked for. Requires that resets only ever
    # happen through this dashboard (data_limit_reset_strategy = no_reset in
    # Marzban); if Marzban itself auto-resets used_traffic between syncs, the
    # accrued-but-unbilled amount at that moment needs to be caught by
    # sync_job.py's external-reset detection rather than this field alone.
    usage_baseline: int = 0
    usage_baseline_at: Optional[datetime] = None

    # prepay's equivalent of usage_baseline: how much of this account's
    # CURRENT data_limit (package size) has already been charged for.
    # "prepay" means paying for the package that was SOLD, not metering
    # consumption within it — billable = data_limit - billed_data_limit,
    # never used_traffic. Kept as a separate field rather than reusing
    # usage_baseline: sync_job.py's external-usage-reset detection updates
    # usage_baseline whenever used_traffic drops unexpectedly, which has
    # nothing to do with how much of a PACKAGE has been paid for — conflating
    # the two would let an unrelated usage event silently corrupt package
    # billing for a prepay account.
    billed_data_limit: int = 0

    # Baseline captured ONCE, immutably, the moment this account is first observed
    # locally (dashboard create, or sync discovering a pre-existing Marzban user) —
    # never touched again after that (unlike usage_baseline, which rolls forward on
    # every settle). Exists solely so the monthly-average-usage estimate measures
    # usage actually OBSERVED by this dashboard, not a Marzban account's entire
    # pre-existing history misattributed to however many days it's been since sync
    # first saw it (see routers/accounts.py's enrich_accounts).
    first_seen_traffic: int = 0
    first_seen_traffic_at: Optional[datetime] = None

    # Marzban's own subscription_url for this user, captured at creation and
    # refreshed by the sync job. Mirrored (not re-derived) because the token in
    # it is generated by Marzban and cannot be recomputed here — without this
    # column, re-sending a customer their link or QR would mean a live Marzban
    # round-trip per account, which a 30-account batch turns into 30 calls.
    # Stored as whatever Marzban returned (relative or absolute); resolving it
    # to a public URL happens at the point of use, in services.resolve_subscription_url,
    # so a later change to MARZBAN_SUBSCRIPTION_BASE_URL fixes every stored row
    # at once instead of needing a backfill.
    subscription_url: Optional[str] = None

    # Marzban's own "last connected" timestamp, mirrored by the sync job —
    # used to derive whether this account is currently online (see
    # services.ONLINE_THRESHOLD_SECONDS) for the online-accounts trend chart.
    online_at: Optional[datetime] = None

    # Opt-OUT flag: defaults True so every existing and newly-created account
    # keeps today's behavior unless someone explicitly turns it off. Read only
    # by sync_job.py's near-quota/near-expiry auto-queue (prepay-only to begin
    # with) — a comp/test/staff account, or one the operator wants to renew by
    # hand every time, sets this False once and stays excluded from then on,
    # including when created as part of a bulk batch (see bulk_accounts.py).
    auto_renew_enabled: bool = True

    created_at: datetime = Field(default_factory=utcnow)

    # SOFT delete only — never a real DELETE. LedgerEntry/AccountEvent/
    # QueuedPlan rows keep pointing at this account_id, and the whole point
    # of the append-only ledger is that history is never destroyed just
    # because the thing it was about is gone. NULL = alive. Set the moment a
    # Delegate deletes it (see routers/delegate.py) — the Marzban user is
    # ALREADY gone by then, so this is a local record-keeping flag, not
    # something that ever gets undone.
    #
    # sync_job.py needs no special-casing for this: it reconciles from
    # Marzban's own user list outward, so an account with no Marzban user
    # left simply stops appearing there and is never touched again —
    # identical to what already happens if an operator deletes a user
    # directly in the Marzban panel, outside this dashboard entirely.
    deleted_at: Optional[datetime] = None


class LedgerEntry(SQLModel, table=True):
    """Append-only money ledger. Never update/delete a row to fix a balance —
    insert a correcting entry instead, so the audit trail stays intact."""

    id: Optional[int] = Field(default=None, primary_key=True)
    type: LedgerType = Field(index=True)
    amount: float
    date: datetime = Field(default_factory=utcnow, index=True)

    customer_id: Optional[int] = Field(default=None, foreign_key="customer.id", index=True)
    group_id: Optional[int] = Field(default=None, foreign_key="group.id", index=True)
    account_id: Optional[int] = Field(default=None, foreign_key="account.id", index=True)

    note: Optional[str] = None
    source: LedgerSource = LedgerSource.web

    # How many GB this charge BILLS for — the package/remainder/top-up size
    # sold (prepay) or the metered usage billed (payg). A structured column,
    # NOT parsed back out of `note` text: same reasoning as
    # MonthlySettlementBatch's docstring — notes are written for humans and
    # drift; a column is queryable and sum-able (MoneyBook's gb totals).
    # Deliberately left NULL on rows that predate it, with NO note-parsing
    # backfill: a wrong number reconstructed from prose is worse than an
    # honest blank. NULL means "unknown", never zero.
    gb_amount: Optional[float] = None

    # How many GB of actual usage this charge ATTRIBUTES to itself — the
    # meter's reading at the charge's checkpoint minus what earlier charges
    # in the same meter epoch already took (see
    # services.attributable_consumed_gb). For payg this equals gb_amount
    # (payg bills exactly what was consumed); for prepay they differ — a
    # 20GB package billed up front that the customer burned 18GB of carries
    # gb_amount=20, consumed_gb=18. Computed from live meter state at charge
    # time, never user-inputtable. NULL on manual entries and on rows
    # predating the column.
    consumed_gb: Optional[float] = None

    # The Toman value of consumed_gb at THIS charge's own rate — recorded
    # next to it because rates change over time, and reconstructing a
    # historical rate from amount/gb_amount would inherit both fields'
    # rounding noise. For payg charges this equals `amount`. NULL whenever
    # consumed_gb is NULL (nothing consumed to value).
    consumed_amount: Optional[float] = None

    # Which operator posted this entry — the raw login username from the JWT
    # (the same string require_auth returns). Set ONLY on web-sourced rows:
    # sync/bot/shop/delegate entries already carry their own `source` label
    # and no per-operator identity applies to them. NULL on those, on manual
    # entries posted outside the app (honest blank over a guess), and on
    # rows predating the column — same convention as gb_amount.
    created_by: Optional[str] = None


class AppSettings(SQLModel, table=True):
    """Single-row table (id is always 1) for dashboard-wide settings — currently
    just the default rate used when neither an account nor its group has one
    set. A real table (not a hardcoded default) so it's editable from the UI."""

    id: Optional[int] = Field(default=1, primary_key=True)
    default_rate_per_gb: Optional[float] = None
    # "1405-05" — the last Jalali (year, month) the payg monthly settlement
    # job successfully completed for. NOT "did it run today" — this is what
    # makes a failed attempt self-retry on every later day instead of
    # silently skipping the rest of that month (see payg_monthly_job.py's
    # _target_settlement_period): the target period doesn't advance to the
    # next month until this one actually succeeds.
    last_payg_monthly_settlement: Optional[str] = None


class RateChange(SQLModel, table=True):
    """Structured audit record for every billing-rate change across all three
    scopes of the effective_rate chain (account rate → group rate →
    dashboard-wide default). Born from a real investigation blocker: "was
    this customer's rate ever temporarily 0?" was unanswerable, because all
    three rate fields are overwrite-in-place — with this table the answer is
    one query.

    A dedicated table, not AccountEvent rows, for two deliberate reasons:
    AccountEvent.account_id is NOT nullable (a global default-rate change has
    no account to attach to, and relaxing it would mean rebuilding the
    accountevent table on the live SQLite DB), and prose details aren't
    queryable — only a human reading history is, which is exactly when this
    question gets asked.

    old_rate/new_rate are Optional on purpose: NULL means "unset", which has
    real meaning in the fallback chain (an unset account rate falls through
    to the group's, an unset group rate to the default) — never rewritten to
    0, which would read as "free" rather than "inherited".

    created_by follows the operator-attribution convention
    (LedgerEntry.created_by): web-sourced changes only, NULL otherwise."""

    id: Optional[int] = Field(default=None, primary_key=True)
    scope: str  # "account" | "group" | "default"
    account_id: Optional[int] = Field(default=None, foreign_key="account.id", index=True)
    group_id: Optional[int] = Field(default=None, foreign_key="group.id", index=True)
    old_rate: Optional[float] = None
    new_rate: Optional[float] = None
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow, index=True)


class AccountEvent(SQLModel, table=True):
    """Audit trail for direct Marzban actions (time/quota changes), since the
    field itself lives in Marzban and isn't duplicated here as an editable value."""

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="account.id", index=True)
    action: str  # "extend_expire" | "reduce_expire" | "set_data_limit" | "create"
    detail: str
    date: datetime = Field(default_factory=utcnow, index=True)
    source: LedgerSource = LedgerSource.web

    # Which operator made this happen — raw login username from the JWT, set
    # only on web-sourced events (same rules as LedgerEntry.created_by).
    # Sync/bot/delegate events carry their own `source` label instead.
    created_by: Optional[str] = None


class MonthlySettlementBatch(SQLModel, table=True):
    """One row per group/standalone-account settled by a single monthly payg
    run (see payg_monthly_job.py) — lets the dashboard's Monthly Settlements
    page show exactly who was billed this cycle and hasn't paid yet, without
    parsing ledger note strings, and gives "mark as paid" something precise
    to point at (this specific settlement's amount, not the entity's whole
    balance, which could include unrelated debt from something else)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    # "1405-05" — same format as AppSettings.last_payg_monthly_settlement.
    jalali_period: str = Field(index=True)
    # Exactly one of these is set — same "group XOR standalone account"
    # shape used throughout (see routers/groups.py vs routers/accounts.py).
    group_id: Optional[int] = Field(default=None, foreign_key="group.id", index=True)
    account_id: Optional[int] = Field(default=None, foreign_key="account.id", index=True)
    # Snapshot at settle time — a rename afterward shouldn't rewrite history.
    display_name: str
    billable_gb: float
    amount: float
    settled_at: datetime = Field(default_factory=utcnow)
    marked_paid_at: Optional[datetime] = None


class OnlineSnapshot(SQLModel, table=True):
    """One point in the online-accounts-count trend — written as a side effect
    of the regular sync job (every sync_interval_seconds), not a separate
    poller, since Marzban has no historical online-count endpoint of its own
    and a dedicated poller would mean extra Marzban logins/requests on top of
    the ones sync already makes. Trend granularity is therefore exactly the
    sync interval — documented, not silently assumed, in the reports router."""

    id: Optional[int] = Field(default=None, primary_key=True)
    recorded_at: datetime = Field(default_factory=utcnow, index=True)
    online_count: int
    total_accounts: int


class QueuedPlanStatus(str, Enum):
    pending = "pending"
    activated = "activated"
    cancelled = "cancelled"


class QueuedPlan(SQLModel, table=True):
    """A plan waiting to be activated on an account when its current plan
    ends (status becomes 'limited' or 'expired' in Marzban). One account
    can have at most one PENDING plan at a time — enforced at the API level,
    not as a DB constraint (so the history of activated/cancelled plans is
    preserved for audit).

    When the sync job detects status='limited' or 'expired' and a pending
    QueuedPlan exists for that account, it:
      1. Auto-settles the OLD plan (posts a charge for any unbilled amount)
      2. Calls Marzban API to set new data_limit + expire + reset usage
      3. Updates the local Account fields
      4. Marks this plan as 'activated'
      5. Logs an AccountEvent for the audit trail
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="account.id", index=True)

    data_limit_gb: float          # e.g. 30.0 = 30 GB
    duration_days: int            # e.g. 30 = 30 days from activation moment
    # None = leave the account's billing_mode exactly as it is at activation
    # time (not as it was when this was queued — an operator could still
    # change it via BillingSection in between). Set = switch it as part of
    # activation, e.g. a prepay account moving to payg for its next cycle.
    billing_mode: Optional[BillingMode] = None

    created_at: datetime = Field(default_factory=utcnow)
    activated_at: Optional[datetime] = None
    status: QueuedPlanStatus = QueuedPlanStatus.pending


# ══════════════════════════════════════════════════════ the self-serve shop
#
# A SECOND, SEPARATE money system, deliberately not reusing LedgerEntry.
#
# LedgerEntry answers "how much does this reseller customer OWE me" — debt
# accrued and settled after the fact. A shop wallet answers the opposite
# question: "how much has this person already PAID me that they haven't spent
# yet" — credit held in advance. Both are money, but they are not the same
# quantity, and every roll-up in services.py (account -> group -> customer)
# assumes every row it sums is the first kind. Putting prepaid wallet credit
# into that table would make every customer balance on the dashboard wrong,
# in the same shape as the bug documented in AGENTS.md §4.3 where a CHARGE
# posted to cancel a CREDIT silently erased money a customer was owed.
#
# So: ShopWalletEntry is its own append-only ledger, summed by its own
# function, and services.py never reads it. A ShopUser MAY be linked to a
# Customer for reporting, but that link carries no money in either direction.
#
# AMOUNTS ARE WHOLE TOMAN, stored as int, everywhere in this section. The
# reseller-side ledger uses float for historical reasons; a wallet is
# different because a balance is repeatedly added to and subtracted from, and
# float drift there eventually shows a customer a balance that doesn't match
# the sum of what they can see. Toman has no subunit in practice, so int
# costs nothing and removes the problem instead of managing it.


class ShopTopupStatus(str, Enum):
    pending = "pending"      # receipt uploaded, waiting for the operator
    approved = "approved"    # operator confirmed; wallet credited
    rejected = "rejected"    # operator declined; wallet untouched


class ShopOrderStatus(str, Enum):
    # An order the customer has chosen but not yet paid for. This exists so a
    # first-time buyer can pick a plan BEFORE being asked for money — the
    # top-up they then send is bound to this order, and approving it both
    # credits the wallet and delivers the plan in one motion.
    #
    # Without this state the customer had to come back after approval and buy
    # again, and the most common outcome was that they didn't: they believed
    # sending the receipt WAS the purchase, so the money sat in a wallet and
    # the subscription was never collected. An order in this state has taken
    # no money and holds nothing; it is a stated intention, safe to abandon.
    awaiting_payment = "awaiting_payment"
    # Paid for. The wallet debit and the order row are written in the same
    # transaction, so from here on an order always exists already paid for.
    # What can still fail after that is provisioning.
    provisioning = "provisioning"
    delivered = "delivered"
    # Wallet was refunded — see shop_service.refund_order. There is
    # deliberately no separate "refunded" state: nothing set it, so it was a
    # value the UI rendered a badge for and the code could never produce. A
    # state machine with a member no transition reaches is a place for a
    # future reader to assume behaviour that does not exist. If an
    # operator-initiated refund after delivery is ever added, add the state
    # together with the transition that writes it.
    failed = "failed"


class ShopWalletEntryType(str, Enum):
    topup = "topup"          # + operator-approved card-to-card payment
    purchase = "purchase"    # − spent on an order
    refund = "refund"        # + returned after a failed or refunded order
    adjust = "adjust"        # ± manual correction by the operator


class ShopUser(SQLModel, table=True):
    """One Telegram user of the customer-facing shop bot.

    Distinct from Customer: a Customer is someone the operator deals with
    personally and bills by hand; a ShopUser is anonymous self-serve traffic
    that pays up front. `customer_id` optionally links the two once the
    operator recognises a shop user as someone they already know — it exists
    for reporting only and moves no money (see the section header above).
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    # Telegram's own numeric user id. Unique because it is the identity the
    # whole shop hangs off — every wallet entry and order is keyed to it, and
    # a duplicate row here would split one person's balance in two.
    telegram_id: int = Field(unique=True, index=True)
    # Snapshot for display. Telegram usernames can change and can be absent
    # entirely, so nothing is ever looked up by these — only shown.
    telegram_username: Optional[str] = None
    display_name: Optional[str] = None
    phone: Optional[str] = None

    # When this person took their free trial. NULL = never taken.
    # A timestamp rather than a bool so the operator can see when, and so a
    # future "one trial per N months" policy has the data it needs without a
    # second migration. The trial is capped per ShopUser, i.e. per Telegram
    # account — deliberately weak, because the alternatives (phone
    # verification, device fingerprinting) cost more trust than the single
    # gigabyte they would protect.
    trial_taken_at: Optional[datetime] = None

    customer_id: Optional[int] = Field(default=None, foreign_key="customer.id", index=True)
    # Blocks buying and topping up, without deleting history. Deleting a
    # ShopUser would orphan their orders' account rows and destroy the audit
    # trail for money they really did pay.
    is_blocked: bool = False

    created_at: datetime = Field(default_factory=utcnow)
    last_seen_at: Optional[datetime] = None


class ShopWalletEntry(SQLModel, table=True):
    """Append-only wallet ledger. Balance is ALWAYS the sum of these rows —
    never a stored field that gets overwritten, for the same reason
    LedgerEntry works that way: a stored balance and its history can disagree,
    and when they do there is no way to tell which one is wrong.

    Never UPDATE or DELETE a row to correct a balance. Insert a compensating
    `adjust` (or `refund`) entry so the trail shows what happened and why.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    shop_user_id: int = Field(foreign_key="shopuser.id", index=True)
    type: ShopWalletEntryType = Field(index=True)
    # SIGNED whole Toman: positive adds to the balance, negative subtracts.
    # The sign lives in the value, not in the type, so a balance is a plain
    # SUM with no per-type branching that a new type could silently escape.
    # (`type` is for display and filtering only.)
    amount: int
    note: Optional[str] = None

    # Which topup or order produced this entry, when one did. Lets the
    # operator answer "what is this line" without parsing the note text.
    topup_id: Optional[int] = Field(default=None, foreign_key="shoptopup.id", index=True)
    order_id: Optional[int] = Field(default=None, foreign_key="shoporder.id", index=True)

    created_at: datetime = Field(default_factory=utcnow, index=True)


class ShopTopup(SQLModel, table=True):
    """A claimed card-to-card payment awaiting the operator's eyes.

    The receipt image is kept as a Telegram file_id rather than downloaded
    bytes: Telegram already stores it, the operator views it inside Telegram
    anyway, and holding customers' bank receipts on this server would make an
    otherwise unremarkable SQLite file worth stealing.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    shop_user_id: int = Field(foreign_key="shopuser.id", index=True)
    # What the user SAYS they paid, in whole Toman. Not trusted — the operator
    # approves an amount explicitly, and may approve a different one (see
    # approved_amount) when the receipt shows something else.
    claimed_amount: int
    # What was actually credited. Null until approved. Kept separate from
    # claimed_amount so "user claimed 500k, operator credited 50k" stays
    # visible forever instead of the claim being overwritten.
    approved_amount: Optional[int] = None

    receipt_file_id: Optional[str] = None

    # A typed tracking code/reference instead of (never both with) a photo —
    # some banking apps make a screenshot awkward, and the operator makes the
    # same manual approve/reject call either way; this only changes what
    # counts as "a receipt was submitted", not who decides whether to credit
    # the wallet. Capped short (see ShopBotTopupRequest) — this is a tracking
    # code, not a place for a customer to write a message.
    receipt_text: Optional[str] = Field(default=None, max_length=300)

    # The order this payment was sent FOR, when the customer chose a plan
    # first. Approving such a top-up credits the wallet and then immediately
    # pays and delivers this order, so the customer never has to come back and
    # buy a second time. NULL = a plain wallet top-up with no plan attached
    # (the repeat-customer path, which is still supported).
    order_id: Optional[int] = Field(default=None, foreign_key="shoporder.id", index=True)

    # Short human-readable code the customer can quote. The point is not
    # lookup — id already does that — it is that the customer HOLDS something
    # the moment their money leaves. Sending cash to a stranger's card and
    # receiving no reference at all is the single biggest driver of "did I
    # just get scammed?" messages.
    reference_code: Optional[str] = Field(default=None, index=True)

    # When the customer was told their payment is taking longer than promised.
    # Once only. The promise ("usually within N minutes") is what makes the
    # wait bearable; breaking it in silence is exactly when an honest shop
    # starts to look like a scam, so the broken promise is announced instead.
    overdue_notified_at: Optional[datetime] = None

    status: ShopTopupStatus = Field(default=ShopTopupStatus.pending, index=True)
    reject_reason: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow, index=True)
    reviewed_at: Optional[datetime] = None


class ShopOrder(SQLModel, table=True):
    """One self-serve purchase. Price and plan are SNAPSHOTTED here at the
    moment of sale — later edits to the shop's rate must never rewrite what
    someone already paid."""

    id: Optional[int] = Field(default=None, primary_key=True)
    shop_user_id: int = Field(foreign_key="shopuser.id", index=True)

    data_limit_gb: float
    duration_days: int
    price: int  # whole Toman, snapshot of the price at purchase time

    status: ShopOrderStatus = Field(default=ShopOrderStatus.provisioning, index=True)
    # Set once Marzban has actually created the user and the local row exists.
    account_id: Optional[int] = Field(default=None, foreign_key="account.id", index=True)
    marzban_username: Optional[str] = None
    error: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow, index=True)
    delivered_at: Optional[datetime] = None

    # When the customer was warned that this plan is about to run out — by
    # time, and by data. One of each, ever, per order: a warning that repeats
    # every sync cycle is spam, and spam is the fastest way to get a bot
    # blocked by the very customer it is trying to keep.
    expiry_warned_at: Optional[datetime] = None
    usage_warned_at: Optional[datetime] = None

    # RENEWAL IN PLACE. When set, this order ADDS its volume and days to an
    # account the customer already has, instead of minting a new one — so the
    # link already imported in their VPN app keeps working and simply grows.
    # Minting a new link per purchase meant the trial's "buy so you don't get
    # cut off" was false (the trial link died anyway), and every later month
    # was a re-import with a dead duplicate left behind in the app.
    extends_account_id: Optional[int] = Field(default=None, foreign_key="account.id", index=True)
    # The account's data_limit / expire as they must read AFTER the extension.
    # Written before Marzban is called, because they are the only EVIDENCE a
    # timed-out modify (or a crash) can be checked against: "is the limit
    # already this high?" is answerable; "did my earlier call land?" is not.
    target_data_limit: Optional[int] = None
    target_expire: Optional[int] = None
    # A small service handed over the moment a receipt arrives, before the
    # operator has approved anything — see shop_service.maybe_grant_provisional
    # for why it exists and what stops it being farmed. price is 0 like a
    # trial, so this flag is what tells the two apart in the operator's list.
    is_provisional: bool = Field(default=False, index=True)


class ShopSettings(SQLModel, table=True):
    """Single-row table (id is always 1), same pattern as AppSettings — the
    shop's configuration lives in the DB so the operator edits it in the
    dashboard, not by redeploying with a changed env var."""

    id: Optional[int] = Field(default=1, primary_key=True)

    # Master switch. Off = the bot answers every purchase and top-up with a
    # "temporarily closed" message instead of silently failing. Defaults OFF
    # so deploying this code does not put a shop live before the operator has
    # set a price and a card number.
    is_open: bool = False

    # Retail price per GB, whole Toman. Deliberately NOT AppSettings'
    # default_rate_per_gb: that one is the reseller-side metered rate used to
    # bill customers the operator knows, and the two would drift together in
    # exactly the wrong way — a retail price change should not silently
    # re-price every existing pay-as-you-go customer.
    price_per_gb: int = 0
    # Bounds on a single self-serve purchase. min stops 0.1GB orders whose
    # price rounds to nothing; max caps what one order can consume before a
    # human looks at it.
    min_gb: float = 5.0
    max_gb: float = 200.0
    # Every shop plan is one month; the operator sells volume, not time.
    # Still a field, not a literal, so changing it is one edit in one place.
    plan_duration_days: int = 30

    # Card-to-card destination shown to the user. No validation beyond
    # non-empty — card number formats vary and a wrong-but-valid-looking
    # number is not something this code can detect anyway.
    card_number: Optional[str] = None
    card_holder: Optional[str] = None

    # Prefix for shop-created Marzban usernames, e.g. "shop" -> shop1, shop2.
    # Kept away from the operator's own naming so a self-serve account can
    # never collide with a family batch.
    username_prefix: str = "shop"

    # Minimum and maximum a single top-up request may claim, whole Toman.
    min_topup: int = 10000
    max_topup: int = 50000000

    # ── Who the customer is buying from ──────────────────────────────────
    # A shop with no name is a commodity. "به ربات فروش اشتراک خوش آمدید"
    # describes a category, not a seller, and the buyer comparing two
    # identical bots has nothing to remember either of them by.
    shop_name: Optional[str] = None
    # Telegram @handle of a real person the customer can message. The flow
    # previously had no way to reach a human at all — which, combined with a
    # subscription link that needs setting up, is indistinguishable from being
    # scammed if anything goes wrong. Stored without the leading @.
    support_handle: Optional[str] = None

    # ── The promise made about the wait ──────────────────────────────────
    # Stated to the customer the moment they send a receipt. A promise that
    # can be kept beats silence that can be defended: the customer has already
    # sent money to a stranger's card, and "we'll tell you when it's done" is
    # not an answer to "when do I stop worrying?".
    # Set it to what the operator can honestly manage on a bad day, not a good
    # one — this number is a commitment, not a hope.
    approval_eta_minutes: int = 30

    # ── The free trial ───────────────────────────────────────────────────
    # The only mechanism that reverses the order of trust in a market with no
    # escrow, no refunds and no ratings: the shop goes first. Off by default,
    # like is_open — the operator turns it on deliberately.
    trial_enabled: bool = False
    # Bridge-the-wait service, delivered when a receipt is uploaded. Off means
    # the customer waits for the operator with nothing, which is the state the
    # whole feature exists to remove.
    provisional_enabled: bool = True
    provisional_gb: float = 1.0
    provisional_hours: int = 24
    trial_gb: float = 1.0
    trial_hours: int = 24


# ══════════════════════════════════════════════════ delegated self-service
#
# A THIRD trust boundary, alongside the operator's own bot (money + full
# Marzban control, one chat id) and the shop bot (the anonymous public,
# wallet-funded, no ledger access at all). A Delegate is neither: a known,
# already-billed reseller customer who is trusted to make and manage their
# OWN Marzban accounts directly, but who must never see or move money — the
# operator's own words were "حساب‌کتاب مالی‌اش سمت من، مدیریت اکانت‌ها سمت
# اون" (the accounting stays with me, account management is comfortable on
# theirs).
#
# Served by delegate_bot/ — its own process and its own Telegram token, on
# the same reasoning as shopbot/ vs bot/ (see shopbot/api_client.py's
# docstring): the process most likely to be pointed at by someone outside
# the operator holds only a narrow key that reaches exactly this router,
# never the Marzban admin credentials or the wallet/charge endpoints.
class Delegate(SQLModel, table=True):
    """One grant of self-service account management, scoped to exactly one
    existing customer or group. Not every customer gets this — it's an
    explicit, per-customer opt-in the operator creates (see bot/handlers/
    delegate_admin.py's /delegate_add), never something a customer can
    request for themselves."""

    id: Optional[int] = Field(default=None, primary_key=True)

    # Exactly one of these — same "customer XOR group" ownership shape used
    # throughout (LedgerEntry, AccountEvent's implicit scope, etc). A group
    # delegate can self-manage every member account under that group; a
    # customer delegate only accounts owned directly by that customer (not
    # grouped) — see delegate_service.py's scoped queries.
    customer_id: Optional[int] = Field(default=None, foreign_key="customer.id", index=True)
    group_id: Optional[int] = Field(default=None, foreign_key="group.id", index=True)

    # Telegram's own numeric id — the identity delegate_bot authenticates
    # by, exact match only (see delegate_bot's docstring for why this is
    # never a fuzzy name lookup, same reasoning as bot/handlers/wallet.py).
    telegram_id: int = Field(unique=True, index=True)
    label: Optional[str] = None
    # Revokes access without losing the row's history (credit_limit, past
    # AccountEvents still point here). The operator's own off-switch.
    is_active: bool = True

    # HARD stop, financial: total posted debt (MoneyBook.customer_posted /
    # group_posted — the real, billed figure, not pending/unbilled usage)
    # at or above this blocks further self-service creates/renews until the
    # customer pays down. None = no cap, i.e. the operator trusts this
    # customer's running tab completely — a deliberate choice the operator
    # makes per delegate, not a default left unexamined.
    credit_limit: Optional[float] = None

    # SOFT stop, operational: not a money guard (that's credit_limit above)
    # — just a backstop on the raw COUNT of creates in a rolling 24h window,
    # so a stuck client or a fat-fingered loop can't mint dozens of Marzban
    # users before anyone notices. Deliberately generous by default; this is
    # a seatbelt, not a rate limit meant to be felt in normal use.
    daily_create_cap: int = 20

    # New accounts are auto-named "{username_prefix}{n}" (see
    # delegate_service.next_delegate_username) rather than letting the
    # delegate type a raw Marzban username: it removes an entire class of
    # input to validate/sanitise, and collisions are resolved the same
    # taken-username-scan bulk_accounts.py already uses for family batches.
    username_prefix: str = "d"
    # Every self-service create uses this many days; asking the delegate to
    # type a duration on every purchase is exactly the friction "راحت باشه"
    # was about. Change it here (operator only) if this customer's plans
    # should run a different length.
    default_duration_days: int = 30

    created_at: datetime = Field(default_factory=utcnow)


class ServerMetric(SQLModel, table=True):
    """One resource sample from a monitored server, pushed every minute by the
    agent in scripts/monitor/ (see that README for the architecture).

    This is diagnostics data, not billing data: nothing here can change what a
    customer is charged. The whole point is the outage timeline — being able to
    line up "node flapped at 21:34" (a MonitorEvent from the panel agent's
    Marzban log watcher) against "CPU steal was 40% right then" (rows here) and
    close the case instead of guessing.

    Numeric columns rather than one JSON blob so history queries stay indexable
    and cheap; `extra` carries the long tail (ping targets, container states)
    that isn't worth its own column. Retention is enforced by the ingest
    endpoint (monitor_metric_retention_days), NOT by the database — the 1GB log
    budget is a hard product constraint and lives in code, not in ops folklore.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(index=True)
    server_id: str = Field(index=True)

    uptime_s: int = 0
    load1: float = 0.0
    load5: float = 0.0
    load15: float = 0.0
    cpu_cores: int = 0

    # cpu_pct includes iowait; steal_pct is broken out because a budget-VPS
    # neighbor squeezing the host shows up as steal long before total CPU
    # looks bad — it was the prime suspect in the 2026-09 flakiness report.
    cpu_pct: float = 0.0
    steal_pct: float = 0.0

    mem_total_mb: int = 0
    mem_used_mb: int = 0
    mem_avail_mb: int = 0
    swap_used_mb: int = 0

    disk_used_pct: float = 0.0

    # Rates over the interval since the previous sample, plus error/drop
    # DELTAS — absolute counters would only tell you the NIC was ever bad.
    net_rx_bps: float = 0.0
    net_tx_bps: float = 0.0
    net_err_delta: int = 0
    net_drop_delta: int = 0

    conntrack_count: int = 0
    conntrack_max: int = 0

    # Retransmitted segments / total outgoing segments over the interval:
    # the single best "is the network path sick right now" number a node
    # can report about its own traffic.
    tcp_retrans_pct: float = 0.0

    extra: str = "{}"


class MonitorEvent(SQLModel, table=True):
    """Something anomalous a monitoring agent noticed, or a state transition
    it observed (node_connection_lost / cpu_steal_high / container_down / ...).

    Events are the outage post-mortem feed: sparse, human-readable, severity
    colored. They are deduplicated at the SOURCE (the agent keeps hysteresis
    latches so one bad minute emits one event, not sixty) — this table does no
    suppression of its own, deliberately: what landed here is what happened.
    Retention enforced on ingest (monitor_event_retention_days), same 1GB
    reasoning as ServerMetric.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(index=True)
    server_id: str = Field(index=True)
    type: str = Field(index=True)
    severity: str = "info"  # info | warn | critical
    detail: str = ""
