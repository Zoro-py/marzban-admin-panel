from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel

from app.models import AccountRole, BillingMode, LedgerSource, LedgerType

# ---- Customer ----------------------------------------------------------


class CustomerCreate(BaseModel):
    name: str
    contact: Optional[str] = None
    is_group_rep: bool = False


class CustomerUpdate(BaseModel):
    name: Optional[str] = None
    contact: Optional[str] = None
    is_group_rep: Optional[bool] = None


class CustomerRead(BaseModel):
    id: int
    name: str
    contact: Optional[str]
    is_group_rep: bool
    created_at: datetime


class CustomerWithBalance(CustomerRead):
    balance: float  # positive = customer owes us (بدهی), negative = we owe them (طلب)
    account_count: int
    # Names of groups this customer is the billing representative for —
    # computed from Group rows (never from the manual is_group_rep flag, which
    # can drift), so the customer list can show real representation without a
    # client-side join against the full group list.
    represented_group_names: list[str] = []


# ---- Group ---------------------------------------------------------------


class GroupCreate(BaseModel):
    name: str
    representative_customer_id: int
    billing_cycle_days: int = 30
    rate_per_gb: Optional[float] = None
    billing_mode: BillingMode = BillingMode.payg


class GroupUpdate(BaseModel):
    name: Optional[str] = None
    billing_cycle_days: Optional[int] = None
    rate_per_gb: Optional[float] = None
    billing_mode: Optional[BillingMode] = None


class GroupSettleRequest(BaseModel):
    """Settling posts a CHARGE — the debt becomes formal/real, not a record
    that payment was received. mark_paid additionally posts a matching
    credit in the same call, netting the balance back to 0 (settled) — for
    the common "they paid me right now" case."""

    mark_paid: bool = False


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
    balance: float
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
    marzban_username: str
    customer_id: Optional[int] = None
    group_id: Optional[int] = None
    role: AccountRole = AccountRole.primary
    rate_per_gb: Optional[float] = None

    expire: Optional[int] = None  # unix timestamp, None = never expires
    data_limit: Optional[int] = None  # bytes, None = unlimited
    data_limit_reset_strategy: str = "no_reset"
    status: str = "active"
    note: Optional[str] = None

    # Pass-through to Marzban; if omitted, server fills MARZBAN_DEFAULT_PROXIES/INBOUNDS.
    proxies: Optional[dict[str, Any]] = None
    inbounds: Optional[dict[str, list[str]]] = None


class AccountRelationshipUpdate(BaseModel):
    customer_id: Optional[int] = None
    group_id: Optional[int] = None
    role: Optional[AccountRole] = None


class AccountBillingUpdate(BaseModel):
    rate_per_gb: Optional[float] = None
    billing_mode: Optional[BillingMode] = None
    clear_rate: bool = False  # explicit clear, since rate_per_gb=None is ambiguous with "unset"


class AccountAdjustRequest(BaseModel):
    """One flexible endpoint for the 'کم/زیاد کردن زمان' live action.
    Deltas are relative (use a negative number to reduce); `set_*` fields win if provided."""

    extend_days: Optional[int] = None
    extend_gb: Optional[float] = None
    set_expire: Optional[int] = None
    set_data_limit_gb: Optional[float] = None
    note: Optional[str] = None


class AccountSettleRequest(BaseModel):
    """Settling posts a CHARGE (the debt becomes real/formal) — it is not, by
    itself, a record that payment was received. mark_paid additionally posts
    a matching credit in the same call, netting the balance back to 0
    (shown as settled, not owed) — for the common "they paid me right now"
    case, so the operator doesn't have to charge, then separately go find
    the payment-recording UI and type the same amount in by hand."""

    mark_paid: bool = False


class AccountResetRequest(BaseModel):
    """Resets usage for a new cycle. `charge_amount`, if given, is posted as a
    charge against the account's customer (the dashboard suggests this amount
    for payg accounts — GET /api/accounts/{id}/invoice — but never posts it
    without the operator confirming/editing it first)."""

    charge_amount: Optional[float] = None
    note: Optional[str] = None


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
    # Balance of whoever actually pays for this account (its customer, or its
    # group's representative customer) — not a per-account ledger slice, since
    # billing is modeled at the customer/group level, not the account level.
    payer_balance: float
    # This account's own unbilled usage-based amount since its last settle —
    # 0 for prepay. payer_balance only reflects REAL posted charges, which for
    # a grouped account stays 0 until the whole group is settled, so without
    # this a member with heavy real usage looks debt-free until then.
    pending_amount: float
    # How this account is ACTUALLY billed: its group's mode when it belongs to
    # one (group settle bills every member by the group's mode regardless of
    # their own field — see services.effective_billing_mode), else its own
    # billing_mode. Distinct from the raw `billing_mode` field above, which is
    # what's actually persisted and what the Billing section edits.
    effective_billing_mode: BillingMode
    monthly_avg_usage_gb: Optional[float]
    usage_confidence: Literal["insufficient_data", "preliminary", "full"]
    usage_sample_days: float


# ---- Ledger ---------------------------------------------------------------


class LedgerCreate(BaseModel):
    type: LedgerType
    amount: float
    customer_id: Optional[int] = None
    group_id: Optional[int] = None
    account_id: Optional[int] = None
    note: Optional[str] = None
    source: LedgerSource = LedgerSource.web


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


class BalanceRead(BaseModel):
    entity_type: Literal["customer", "group"]
    entity_id: int
    total_charge: float
    total_credit: float
    balance: float  # total_charge - total_credit; positive = they owe us
