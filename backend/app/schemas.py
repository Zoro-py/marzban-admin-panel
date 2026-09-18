from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.models import (
    AccountRole,
    BillingMode,
    LedgerSource,
    LedgerType,
    ShopOrderStatus,
    ShopTopupStatus,
    ShopWalletEntryType,
)
from app.bulk_accounts import MAX_BULK_COUNT, MAX_NAME_INDEX

# ---- Customer ----------------------------------------------------------


class CustomerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r".*\S.*")
    contact: Optional[str] = None
    is_group_rep: bool = False


class CustomerUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100, pattern=r"\S")
    contact: Optional[str] = Field(default=None, max_length=120)
    is_group_rep: Optional[bool] = None


class CustomerRead(BaseModel):
    id: int
    name: str
    contact: Optional[str]
    is_group_rep: bool
    created_at: datetime


class CustomerWithBalance(CustomerRead):
    # Roll-up of the accounts they own plus the groups they represent (see
    # services.MoneyBook). POSTED only; prefer net_owed for display.
    balance: float
    # balance + everything not yet invoiced across those same accounts and
    # groups — what this customer owes right now.
    net_owed: float  # positive = customer owes us (بدهی), negative = we owe them (طلب)
    account_count: int
    # Names of groups this customer is the billing representative for —
    # computed from Group rows (never from the manual is_group_rep flag, which
    # can drift), so the customer list can show real representation without a
    # client-side join against the full group list.
    represented_group_names: list[str] = []


# ---- Group ---------------------------------------------------------------


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"\S")
    representative_customer_id: int
    billing_cycle_days: int = Field(default=30, ge=1, le=365)
    rate_per_gb: Optional[float] = Field(default=None, ge=0.0)
    billing_mode: BillingMode = BillingMode.payg


class GroupUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100, pattern=r"\S")
    billing_cycle_days: Optional[int] = Field(default=None, ge=1, le=365)
    rate_per_gb: Optional[float] = Field(default=None, ge=0.0)
    billing_mode: Optional[BillingMode] = None


class GroupSettleRequest(BaseModel):
    """Settling posts a CHARGE — the debt becomes formal/real, not a record
    that payment was received. mark_paid additionally posts a matching
    credit in the same call, netting the balance back to 0 (settled) — for
    the common "they paid me right now" case.

    pay_scope only matters when mark_paid=True: "full" (default) credits
    whatever's owed AFTER this cycle's charge lands (old debt + new charge).
    "prior_only" credits just the balance that existed BEFORE this charge —
    for a customer who's paying off an old cycle right now but hasn't paid
    this new one yet, so today's payment doesn't get silently applied to a
    charge they haven't actually paid for."""

    mark_paid: bool = False
    pay_scope: Literal["full", "prior_only"] = "full"


class InvoiceLine(BaseModel):
    account_id: int
    marzban_username: str
    billable_gb: float
    rate_per_gb: float
    amount: float


class GroupRead(BaseModel):
    id: int
    name: str
    representative_customer_id: int
    billing_cycle_days: int
    rate_per_gb: Optional[float]
    billing_mode: BillingMode
    last_settled_at: Optional[datetime]
    created_at: datetime


class GroupWithBalance(GroupRead):
    # Roll-up of this group's members plus any group-level entry not tied to
    # one member — see services.MoneyBook. POSTED only; prefer net_owed.
    balance: float
    # balance + pending_amount: what the group owes right now, and exactly the
    # sum of the members' own net_owed. Guaranteed to reconcile with the rows
    # shown underneath it because it is literally computed as their sum.
    net_owed: float
    account_count: int
    # Marzban's own used_traffic counter, summed — NOT what should drive
    # billing (see current_cycle_used_bytes for that): Marzban can reset this
    # independently of when WE last settled the group.
    total_used_traffic: int
    # Usage actually accrued since the group's last settlement (sum of each
    # member's lifetime_used_traffic - usage_baseline) — this is what a settle
    # right now would charge, and what item 1 of the operator's feedback
    # asked to see prioritized over a lifetime/reset-prone counter.
    current_cycle_used_bytes: int
    # What settling right now would charge, at each member's effective rate —
    # the "why is balance 0 even though there's real usage" answer: nothing
    # gets charged until an operator explicitly settles or invoices.
    pending_amount: float
    next_due_at: datetime
    is_due: bool


# ---- Account ---------------------------------------------------------------


class AccountCreateRequest(BaseModel):
    marzban_username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    customer_id: Optional[int] = None
    group_id: Optional[int] = None
    role: AccountRole = AccountRole.primary
    rate_per_gb: Optional[float] = Field(default=None, ge=0.0)

    expire: Optional[int] = Field(default=None, ge=0, le=2147483647)  # unix timestamp, None = never expires
    # 10 PB, i.e. "no sane package is this big": an unbounded byte count here
    # reaches Marzban and every later usage calculation unchecked.
    data_limit: Optional[int] = Field(default=None, ge=0, le=10 * 1024 ** 5)  # bytes, None/0 = unlimited
    data_limit_reset_strategy: str = "no_reset"
    status: str = "active"
    note: Optional[str] = None

    # Pass-through to Marzban; if omitted, server fills MARZBAN_DEFAULT_PROXIES/INBOUNDS.
    proxies: Optional[dict[str, dict[str, Any]]] = None
    inbounds: Optional[dict[str, list[str]]] = None


class AccountRelationshipUpdate(BaseModel):
    customer_id: Optional[int] = None
    group_id: Optional[int] = None
    role: Optional[AccountRole] = None


class AccountBillingUpdate(BaseModel):
    rate_per_gb: Optional[float] = Field(default=None, ge=0.0)
    billing_mode: Optional[BillingMode] = None
    clear_rate: bool = False  # explicit clear, since rate_per_gb=None is ambiguous with "unset"
    # None = leave as-is. Gates sync_job.py's near-quota/near-expiry auto-queue
    # only — has no effect on anything else (settle, reset, manual next-plan).
    auto_renew_enabled: Optional[bool] = None


class AccountAdjustRequest(BaseModel):
    """One flexible endpoint for the 'کم/زیاد کردن زمان' live action.
    Deltas are relative (use a negative number to reduce); `set_*` fields win if provided."""

    # Bounded on both sides: these are typed by hand in a dialog, and a slip
    # of the keyboard otherwise writes an expiry in the year 40000 or a
    # package no panel can represent.
    extend_days: Optional[int] = Field(default=None, ge=-3650, le=3650)
    extend_gb: Optional[float] = Field(default=None, ge=-10240, le=10240, allow_inf_nan=False)
    set_expire: Optional[int] = Field(default=None, ge=0, le=2147483647)
    set_data_limit_gb: Optional[float] = Field(default=None, ge=0, le=10240, allow_inf_nan=False)
    note: Optional[str] = Field(default=None, max_length=500)


class AccountSettleRequest(BaseModel):
    """Settling posts a CHARGE (the debt becomes real/formal) — it is not, by
    itself, a record that payment was received. mark_paid additionally posts
    a matching credit in the same call, netting the balance back to 0
    (shown as settled, not owed) — for the common "they paid me right now"
    case, so the operator doesn't have to charge, then separately go find
    the payment-recording UI and type the same amount in by hand.

    pay_scope only matters when mark_paid=True — see GroupSettleRequest for
    the full explanation. "prior_only" credits just the balance that existed
    BEFORE this charge, leaving this cycle's new charge itself outstanding."""

    mark_paid: bool = False
    pay_scope: Literal["full", "prior_only"] = "full"


class AccountResetRequest(BaseModel):
    """Resets usage for a new cycle. `charge_amount`, if given, is posted as a
    charge against the account's customer (the dashboard suggests this amount
    for payg accounts — GET /api/accounts/{id}/invoice — but never posts it
    without the operator confirming/editing it first)."""

    charge_amount: Optional[float] = Field(default=None, ge=0, le=1_000_000_000, allow_inf_nan=False)
    note: Optional[str] = Field(default=None, max_length=500)


class AccountRead(BaseModel):
    id: int
    marzban_username: str
    customer_id: Optional[int]
    group_id: Optional[int]
    role: AccountRole
    rate_per_gb: Optional[float]
    billing_mode: BillingMode
    used_traffic: int
    lifetime_used_traffic: int
    data_limit: Optional[int]
    expire: Optional[int]
    status: Optional[str]
    last_synced_at: Optional[datetime]
    auto_renew_enabled: bool
    created_at: datetime


class AccountEventRead(BaseModel):
    """One entry of the per-account audit trail (AccountEvent) — the account
    inspector's History section shows these interleaved with ledger entries,
    so an operator can answer "what happened to this account?" in one place."""

    id: int
    account_id: int
    action: str
    detail: str
    date: datetime
    source: LedgerSource
    # Raw login username of the operator — web-sourced events only (see
    # LedgerRead.created_by).
    created_by: Optional[str] = None


class AccountRow(AccountRead):
    """Enriched shape for the accounts table — everything item 4/5/6/14 of the
    UI ask needs to sort/filter/display without the frontend re-deriving it
    from three other endpoints."""

    customer_name: Optional[str]
    group_name: Optional[str]
    effective_rate: float
    # Whether effective_rate resolves from an ACTUAL configured value somewhere
    # in the chain, vs. falling through to 0 because nothing was ever set. An
    # operator can legitimately price an account at 0 (comp/free) — that's
    # rate_configured=True, effective_rate=0, distinct from never-configured.
    rate_configured: bool
    # POSTED ledger balance scoped to this account alone — see
    # services.account_scoped_balance for exactly what counts. Prefer
    # net_owed for display; this is the "already invoiced" half of it.
    payer_balance: float
    # This account's own unbilled amount since its last settle. payer_balance
    # only reflects REAL posted charges, which for a grouped account stays 0
    # until the group is settled, so without this a member with heavy real
    # usage looks debt-free until then.
    pending_amount: float
    # THE number to display as "what do they owe me right now":
    # payer_balance + pending_amount. Showing those two side by side instead
    # made a customer who had just paid off their debt look like they still
    # owed the full unbilled amount (a 230,000 credit next to a 243,916
    # pending, when the honest answer is "13,916 owed"). Netting them is also
    # STABLE ACROSS SETTLING — settle moves an amount from pending into
    # payer_balance, leaving this figure unchanged, which is exactly right:
    # formalizing a bill doesn't change what someone owes.
    net_owed: float
    # How this account is ACTUALLY billed: its group's mode when it belongs to
    # one (group settle bills every member by the group's mode regardless of
    # their own field — see services.effective_billing_mode), else its own
    # billing_mode. Distinct from the raw `billing_mode` field above, which is
    # what's actually persisted and what the Billing section edits.
    effective_billing_mode: BillingMode
    monthly_avg_usage_gb: Optional[float]
    usage_confidence: Literal["insufficient_data", "preliminary", "full"]
    usage_sample_days: float
    # True if a QueuedPlan with status='pending' exists for this account —
    # shown as a small badge in the accounts table so the operator can see at
    # a glance which accounts are covered.
    has_next_plan: bool = False


# ---- Next Plan (Queued Plan) -----------------------------------------------


class NextPlanRequest(BaseModel):
    """Queue a plan to activate when the current plan ends."""

    data_limit_gb: float = Field(gt=0, le=10240, allow_inf_nan=False, description="Package size in GB")
    duration_days: int = Field(gt=0, le=365, description="Duration in days from activation")
    # None = keep whatever billing_mode the account has at activation time.
    billing_mode: Optional[BillingMode] = None


class NextPlanRead(BaseModel):
    """Response shape for a queued plan."""

    id: int
    account_id: int
    data_limit_gb: float
    duration_days: int
    billing_mode: Optional[BillingMode]
    status: str
    created_at: datetime
    activated_at: Optional[datetime]


# ---- Ledger ---------------------------------------------------------------


class LedgerCreate(BaseModel):
    type: LedgerType
    # NaN slips past the router's `amount <= 0` check (every comparison with
    # NaN is False) and would poison every balance it is summed into.
    amount: float = Field(gt=0, allow_inf_nan=False)
    customer_id: Optional[int] = None
    group_id: Optional[int] = None
    account_id: Optional[int] = None
    note: Optional[str] = None
    source: LedgerSource = LedgerSource.web
    # Optional, caller-supplied GB for manual CHARGE entries (e.g. a recovery
    # charge for a known package size posted by hand). Only meaningful on
    # charges; a credit carrying GB would be nonsense. consumed_gb is
    # deliberately NOT inputtable — it is derived from live meter state by
    # the codebase's own billing flows, never declared (see
    # models.LedgerEntry.consumed_gb).
    gb_amount: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)


class LedgerRead(BaseModel):
    id: int
    type: LedgerType
    amount: float
    date: datetime
    customer_id: Optional[int]
    group_id: Optional[int]
    account_id: Optional[int]
    note: Optional[str]
    source: LedgerSource
    gb_amount: Optional[float] = None
    consumed_gb: Optional[float] = None
    consumed_amount: Optional[float] = None
    # Raw login username of the operator who posted it — set on web-sourced
    # rows only (sync/bot/shop/delegate carry their own source label).
    created_by: Optional[str] = None


class BalanceRead(BaseModel):
    entity_type: Literal["customer", "group", "account"]
    entity_id: int
    total_charge: float
    total_credit: float
    balance: float  # total_charge - total_credit; positive = they owe us
    # GB billed (sold) and GB of actual usage attributed to charges within
    # the same window/scope as `balance`. NULL means "no charge in this
    # window carries a known GB figure" (entries predating GB tracking, or
    # manual money-only entries) — displayed as "—", never as 0.
    gb_charged: Optional[float] = None
    gb_consumed: Optional[float] = None
    # Toman equivalents, same window/scope:
    #   charged_amount  — gross sum of charge amounts (NOT netted against
    #                     credits; every charge counts, even GB-less legacy
    #                     ones).
    #   consumed_amount — the consumed GB valued at each charge's own rate,
    #                     recorded at charge time. An estimate of what the
    #                     consumed data was worth, NOT what they owe.
    consumed_amount: Optional[float] = None
    charged_amount: Optional[float] = None
    # Gross sum of credit (payment) amounts in the same window/scope, riding
    # the same `since` boundary as the figures above. Pairs with
    # charged_amount so a settle+payment pair reads as "charged X /
    # credited X" instead of the payment vanishing into the netted balance.
    # None = no credit row in this window — displayed as "—", never as 0.
    credited_amount: Optional[float] = None
    # Live, window-independent (exactly like the money pending figure):
    # usage accrued since each account's current meter epoch started that no
    # charge has attributed yet. The GB sibling of "not invoiced yet".
    gb_pending: Optional[float] = None
    # The money sibling of gb_pending — accrued usage not yet invoiced, at
    # the current effective rate. Same window-blind "right now" semantics:
    # usage can't be attributed to a time sub-range (Marzban exposes no
    # per-interval bytes), so this does not respect `since` either. Shown
    # next to gb_pending so the widget states the full picture of a period:
    # "posted 43,288 + accruing 153,115", not just the settled slice.
    pending_amount: Optional[float] = None


# ---- Bulk ("family") account creation ----------------------------------


class BulkAccountCreateRequest(BaseModel):
    """One base name + a count, expanded into base1, base2, … — the "family
    package" flow (one operator instruction, N identical accounts).

    Plan size is expressed in DAYS and GB here, not as the absolute unix
    `expire` AccountCreateRequest takes. Both front-ends already convert
    days -> timestamp themselves, and doing it once here instead of once per
    item is what guarantees every account in a batch carries the exact same
    expiry rather than drifting by however long the batch took to run.
    """

    # Two chars minimum so a stray single keystroke can't quietly claim a huge
    # namespace. The max leaves room for the numeric suffix; the real check is
    # bulk_accounts.validate_base_name, which knows the batch's highest index.
    base_name: str = Field(min_length=2, max_length=28, pattern=r"^[a-zA-Z0-9_]+$")
    count: int = Field(ge=1, le=MAX_BULK_COUNT)
    # None = continue from the highest existing suffix for this base name.
    # Set = use exactly these numbers; names already taken are reported as
    # skipped rather than shifting the rest along (see plan_bulk_usernames).
    start_index: Optional[int] = Field(default=None, ge=1, le=MAX_NAME_INDEX)

    customer_id: Optional[int] = None
    group_id: Optional[int] = None
    role: AccountRole = AccountRole.primary
    rate_per_gb: Optional[float] = Field(default=None, ge=0.0)

    # None = never expires / unlimited, matching Marzban's own semantics for
    # expire=None and data_limit=None. Not 0 — 0 would mean "already expired".
    expire_days: Optional[int] = Field(default=None, ge=1, le=3650)
    data_limit_gb: Optional[float] = Field(default=None, gt=0.0, le=10240.0)
    data_limit_reset_strategy: str = "no_reset"
    status: str = "active"
    note: Optional[str] = None

    proxies: Optional[dict[str, dict[str, Any]]] = None
    inbounds: Optional[dict[str, list[str]]] = None

    # Off only for a caller that wants the links in the HTTP response without
    # filling the operator's chat (e.g. re-running a preview-driven flow).
    notify: bool = True

    # Applied to every account created in this batch. Default True (today's
    # behavior); turn off for a batch the operator always wants to renew by
    # hand (comp accounts, staff, family) — see Account.auto_renew_enabled.
    auto_renew_enabled: bool = True


class BulkAccountPlannedName(BaseModel):
    index: int
    marzban_username: str
    already_exists: bool


class BulkAccountPreview(BaseModel):
    """What POST /bulk would do, without doing it. Exists so the operator sees
    the exact usernames before N irreversible Marzban creates, rather than
    after."""

    base_name: str
    start_index: int
    names: list[BulkAccountPlannedName]
    will_create: int
    will_skip: int


class BulkAccountItem(BaseModel):
    marzban_username: str
    # created           — exists in Marzban AND tracked locally
    # skipped_exists    — the name was already taken; nothing was done
    # created_untracked — created in Marzban but the local row failed to save.
    #                     Needs operator attention: the account is live and
    #                     billable but invisible to this dashboard until the
    #                     sync job adopts it.
    # failed            — nothing was created
    status: Literal["created", "skipped_exists", "created_untracked", "failed"]
    account_id: Optional[int] = None
    subscription_url: Optional[str] = None
    error: Optional[str] = None


class BulkAccountCreateResult(BaseModel):
    base_name: str
    start_index: int
    requested: int
    created: int
    skipped: int
    failed: int
    items: list[BulkAccountItem]
    # False when BOT_TOKEN/BOT_ADMIN_CHAT_ID aren't configured or notify=False
    # — the accounts still exist, the QR messages just aren't coming, and the
    # caller needs to say so rather than let the operator wait for a chat that
    # stays silent.
    notifications_queued: bool
    # Set when the batch stopped early because Marzban became unreachable
    # mid-run. Everything before it was still really created.
    aborted_reason: Optional[str] = None


# ---- Self-serve shop ----------------------------------------------------
#
# Every monetary field here is WHOLE TOMAN as an int, matching the shop
# models. Do not widen any of them to float to "match the rest of the API" —
# the reseller ledger's floats and the wallet's ints are different quantities
# (see the shop section header in models.py), and a float wallet balance
# drifts away from the sum of the entries the customer can see.


class ShopSettingsRead(BaseModel):
    id: int
    is_open: bool
    price_per_gb: int
    min_gb: float
    max_gb: float
    plan_duration_days: int
    card_number: Optional[str]
    card_holder: Optional[str]
    username_prefix: str
    min_topup: int
    max_topup: int
    shop_name: Optional[str] = None
    support_handle: Optional[str] = None
    approval_eta_minutes: int = 30
    provisional_enabled: bool = True
    provisional_gb: float = 1.0
    provisional_hours: int = 24
    trial_enabled: bool = False
    trial_gb: float = 1.0
    trial_hours: int = 24


class ShopSettingsUpdate(BaseModel):
    # Every field optional, and the router applies only the ones actually
    # sent (`exclude_unset`) — a PATCH that omits price_per_gb must not reset
    # the price to a default.
    is_open: Optional[bool] = None
    price_per_gb: Optional[int] = Field(default=None, ge=0)
    min_gb: Optional[float] = Field(default=None, gt=0)
    max_gb: Optional[float] = Field(default=None, gt=0, le=10240)
    plan_duration_days: Optional[int] = Field(default=None, ge=1, le=3650)
    card_number: Optional[str] = Field(default=None, max_length=64)
    card_holder: Optional[str] = Field(default=None, max_length=100)
    username_prefix: Optional[str] = Field(default=None, min_length=2, max_length=12, pattern=r"^[a-zA-Z0-9_]+$")
    min_topup: Optional[int] = Field(default=None, ge=0)
    max_topup: Optional[int] = Field(default=None, ge=1)
    shop_name: Optional[str] = Field(default=None, max_length=60)
    # Stored without the leading @ — the bot adds it. Accepting one and
    # stripping it means an operator who types @myshop and one who types
    # myshop get the same working link, instead of one of them shipping
    # "@@myshop" to every customer.
    support_handle: Optional[str] = Field(default=None, max_length=40)
    # Capped at a day: this is a promise shown to someone who has already sent
    # money, and "we'll look at it within a week" is not a promise that keeps
    # anyone waiting — it loses the sale outright.
    approval_eta_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    provisional_enabled: Optional[bool] = None
    # Deliberately small caps: this is service given away before any money is
    # confirmed, so the most a mistyped settings page can cost is a few GB.
    provisional_gb: Optional[float] = Field(default=None, gt=0, le=20)
    provisional_hours: Optional[int] = Field(default=None, ge=1, le=168)
    trial_enabled: Optional[bool] = None
    trial_gb: Optional[float] = Field(default=None, gt=0, le=100)
    trial_hours: Optional[int] = Field(default=None, ge=1, le=720)


class ShopUserRead(BaseModel):
    id: int
    telegram_id: int
    telegram_username: Optional[str]
    display_name: Optional[str]
    phone: Optional[str]
    customer_id: Optional[int]
    is_blocked: bool
    balance: int
    created_at: datetime
    last_seen_at: Optional[datetime]


class ShopUserUpdate(BaseModel):
    is_blocked: Optional[bool] = None
    customer_id: Optional[int] = None
    display_name: Optional[str] = Field(default=None, max_length=100)
    phone: Optional[str] = Field(default=None, max_length=32)


class ShopWalletEntryRead(BaseModel):
    id: int
    shop_user_id: int
    type: ShopWalletEntryType
    amount: int  # signed
    note: Optional[str]
    topup_id: Optional[int]
    order_id: Optional[int]
    created_at: datetime


class ShopWalletAdjustRequest(BaseModel):
    # Signed, and no ge/le bound beyond sanity: a manual correction sometimes
    # legitimately has to be large (refunding a mistaken 5,000,000 T credit).
    # The router rejects exactly zero, which is the only value that is always
    # a mistake.
    amount: int = Field(ge=-1_000_000_000, le=1_000_000_000)
    note: Optional[str] = Field(default=None, max_length=200)


class ShopTopupRead(BaseModel):
    id: int
    shop_user_id: int
    claimed_amount: int
    approved_amount: Optional[int]
    receipt_file_id: Optional[str]
    receipt_text: Optional[str] = None
    status: ShopTopupStatus
    reject_reason: Optional[str]
    created_at: datetime
    reviewed_at: Optional[datetime]
    # The code the customer was given and will quote back. Shown in the
    # operator's pending list so a "what happened to A7K2?" message can be
    # answered by scanning the screen rather than searching.
    reference_code: Optional[str] = None
    # Set when this payment was sent FOR a plan: approving it delivers that
    # plan too. The operator needs to see the difference, because approving a
    # short amount leaves the customer waiting for a subscription.
    order_id: Optional[int] = None
    # Denormalised for display so the dashboard's pending list doesn't need a
    # second request per row to say who it's from.
    telegram_id: Optional[int] = None
    display_name: Optional[str] = None


class ShopTopupDecision(BaseModel):
    # None on approve = credit exactly what the customer claimed. Set it to
    # credit a different figure when the receipt disagrees with the claim.
    amount: Optional[int] = Field(default=None, gt=0)
    reason: Optional[str] = Field(default=None, max_length=200)


class ShopOrderRead(BaseModel):
    id: int
    shop_user_id: int
    data_limit_gb: float
    duration_days: int
    price: int
    status: ShopOrderStatus
    account_id: Optional[int]
    marzban_username: Optional[str]
    error: Optional[str]
    created_at: datetime
    delivered_at: Optional[datetime]
    is_provisional: bool = False
    telegram_id: Optional[int] = None
    display_name: Optional[str] = None


# ---- shop bot (scoped key, not the dashboard JWT) ----


class ShopBotSessionRequest(BaseModel):
    telegram_id: int
    telegram_username: Optional[str] = Field(default=None, max_length=64)
    display_name: Optional[str] = Field(default=None, max_length=100)


class ShopBotPhoneRequest(BaseModel):
    telegram_id: int
    # Whatever Telegram's own contact-share button hands back — not
    # normalised or validated beyond a sane length, since this is only ever
    # for the operator to look at, never dialled by code.
    phone: str = Field(min_length=5, max_length=32)


class ShopBotSession(BaseModel):
    shop_user_id: int
    is_blocked: bool
    balance: int
    is_open: bool
    price_per_gb: int
    min_gb: float
    max_gb: float
    plan_duration_days: int
    card_number: Optional[str]
    card_holder: Optional[str]
    min_topup: int
    max_topup: int

    # Who the customer is buying from. A shop with no name and no reachable
    # human is indistinguishable from every other bot asking for a card
    # transfer, which is the whole competitive problem.
    shop_name: Optional[str] = None
    support_handle: Optional[str] = None
    # The promise made about how long approval takes. Sent to the bot rather
    # than hardcoded there so the operator can change what they promise
    # without a redeploy of a separate process.
    approval_eta_minutes: int = 30

    # Whether to show the trial button at all, and whether THIS customer can
    # still take it. Two separate booleans on purpose: "the shop doesn't offer
    # trials" and "you have already had yours" need different sentences, and a
    # single flag would make the button vanish for returning customers with no
    # explanation of where it went.
    trial_enabled: bool = False
    trial_available: bool = False
    trial_gb: float = 0.0
    trial_hours: int = 0

    # Whether we already have their phone number — lets the bot ask exactly
    # once (after they've actually bought something, never on /start; see
    # take_trial/_confirm_wallet_purchase in handlers/shop.py) instead of
    # re-prompting every session.
    phone: Optional[str] = None


class ShopBotOrderAction(BaseModel):
    """Who the action is for.

    The bot key proves a request came from the shop bot; it says nothing about
    WHICH customer it is for. Anything addressed by order id therefore carries
    the asker's telegram_id too, so a wrong id in the bot can only ever act on
    that customer's own orders.
    """

    telegram_id: int


class ShopBotPurchaseRequest(BaseModel):
    telegram_id: int
    # Bounded here as well as in shop_service.validate_purchase_request: this
    # stops an absurd value (1e9 GB) reaching the pricing multiplication at
    # all, while the service-level check enforces the operator's own,
    # narrower min/max that can change at runtime.
    data_limit_gb: float = Field(gt=0, le=10240)


class ShopPurchaseResult(BaseModel):
    order_id: int
    marzban_username: Optional[str]
    data_limit_gb: float
    duration_days: int
    price: int
    subscription_url: Optional[str]
    balance: int
    status: ShopOrderStatus


class ShopBotTopupRequest(BaseModel):
    telegram_id: int
    claimed_amount: int = Field(gt=0, le=1_000_000_000)
    receipt_file_id: Optional[str] = Field(default=None, max_length=256)
    # Alternative to receipt_file_id, never both (the bot sends exactly one —
    # see handlers/shop.py). A typed tracking code, not free text: the bot
    # itself rejects anything that doesn't look like one before ever calling
    # this endpoint (see _looks_like_receipt_text), but the cap here is the real
    # backstop since nothing about this field is trusted just because it came
    # from the bot process.
    receipt_text: Optional[str] = Field(default=None, max_length=300)
    # The plan this payment was sent FOR, when the customer chose first.
    # Approving such a top-up credits the wallet AND delivers the plan, so the
    # customer never has to come back and place the order a second time.
    # None = a plain wallet top-up, which is still supported for anyone who
    # wants to pre-load credit.
    order_id: Optional[int] = None


class ShopOrderIntent(BaseModel):
    """What the bot needs after the customer picks a volume but before any
    money moves — enough to choose between a one-tap confirm and a payment
    request, without a second round-trip."""

    order_id: int
    data_limit_gb: float
    duration_days: int
    price: int
    balance: int
    # Never negative: a wallet that more than covers the plan yields 0 here and
    # `payable_from_wallet` true. Showing a customer a negative amount owed was
    # one of the concrete defects in the previous flow.
    shortfall: int
    payable_from_wallet: bool
    card_number: Optional[str] = None
    card_holder: Optional[str] = None
    approval_eta_minutes: int


class ShopBotAccountRow(BaseModel):
    order_id: int
    marzban_username: str
    data_limit_gb: float
    used_traffic: int
    data_limit: Optional[int]
    expire: Optional[int]
    status: Optional[str]
    subscription_url: Optional[str]
    created_at: datetime


# ══════════════════════════════════════════════════ delegated self-service
# See models.py's Delegate docstring for the trust-boundary reasoning.


class DelegateCreateRequest(BaseModel):
    """Operator-only (POST /api/delegate). Exactly one of customer_id/
    group_id when CREATING — enforced in the router, not here, so the 400
    can name which rule was broken rather than pydantic's generic
    validation error.

    Upserts by telegram_id, but PARTIALLY on an existing row: the router
    only touches fields actually present in the request
    (`exclude_unset=True`), so a caller that sends just {telegram_id,
    credit_limit} — see bot/handlers/delegate_admin.py's /delegate_cap —
    leaves every other field on the existing row untouched. Do not rely on
    "the field has a default" to mean "omitting it is safe" on an update;
    it is safe only because the router checks presence, not value."""
    customer_id: Optional[int] = None
    group_id: Optional[int] = None
    telegram_id: int
    label: Optional[str] = Field(default=None, max_length=100)
    # allow_inf_nan=False: float("inf") passes a plain ge=0 check (inf >= 0
    # is True) and would silently mean "no limit" while looking like a real
    # bounded number everywhere it's displayed.
    credit_limit: Optional[float] = Field(default=None, ge=0, le=1_000_000_000, allow_inf_nan=False)
    daily_create_cap: int = Field(default=20, ge=1, le=500)
    username_prefix: str = Field(default="d", min_length=1, max_length=12, pattern=r"^[a-zA-Z0-9_]+$")
    default_duration_days: int = Field(default=30, ge=1, le=3650)


class DelegateRead(BaseModel):
    id: int
    customer_id: Optional[int]
    group_id: Optional[int]
    scope_name: str
    telegram_id: int
    label: Optional[str]
    is_active: bool
    credit_limit: Optional[float]
    daily_create_cap: int
    username_prefix: str
    default_duration_days: int
    created_at: datetime


class DelegateSessionRequest(BaseModel):
    telegram_id: int


class DelegateSession(BaseModel):
    """Everything delegate_bot needs to draw its menu for one delegate, in
    one call — same reasoning as ShopBotSession: splitting this into several
    round-trips would make one tap depend on all of them succeeding."""
    delegate_id: int
    label: Optional[str]
    scope_name: str  # the customer's or group's display name, for the menu header
    default_duration_days: int
    # Quick-pick GB presets shown as buttons — mirrors the shop bot's
    # QUICK_VOLUMES, purely a UI convenience computed server-side so the
    # bot doesn't hardcode a list that can drift from what's sane for this
    # delegate's own typical package sizes.
    quick_volumes_gb: list[int]


class DelegateAccountRow(BaseModel):
    id: int
    marzban_username: str
    used_traffic: int
    data_limit: Optional[int]
    expire: Optional[int]
    status: Optional[str]
    subscription_url: Optional[str]
    created_at: datetime


class DelegateAccountCreateRequest(BaseModel):
    telegram_id: int
    data_limit_gb: float = Field(gt=0, le=10240)


class DelegateAccountRenewRequest(BaseModel):
    telegram_id: int
    extend_gb: float = Field(gt=0, le=10240)
    # None = the delegate's own default_duration_days (the common case —
    # renewing is "give it more of the usual"); explicit only if a future
    # UI ever wants to offer a different length.
    extend_days: Optional[int] = Field(default=None, gt=0, le=3650)


class DelegateAccountDeleteRequest(BaseModel):
    telegram_id: int
