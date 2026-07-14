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
    representative_customer_id: int = Field(foreign_key="customer.id")
    billing_cycle_days: int = 30
    rate_per_gb: Optional[float] = None
    billing_mode: BillingMode = BillingMode.payg
    last_settled_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)


class Account(SQLModel, table=True):
    """One Marzban user, mirrored locally with ownership + a synced usage snapshot."""

    id: Optional[int] = Field(default=None, primary_key=True)
    marzban_username: str = Field(unique=True, index=True)

    customer_id: Optional[int] = Field(default=None, foreign_key="customer.id")
    group_id: Optional[int] = Field(default=None, foreign_key="group.id")
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

    # `lifetime_used_traffic` value as of the last pay-as-you-go settlement (individual
    # or as part of a group). Billable usage for the current cycle = lifetime - baseline.
    # Using lifetime_used_traffic (monotonic, survives Marzban data_limit resets) rather
    # than used_traffic avoids silently re-billing usage across a reset boundary.
    usage_baseline: int = 0
    usage_baseline_at: Optional[datetime] = None

    # Baseline captured ONCE, immutably, the moment this account is first observed
    # locally (dashboard create, or sync discovering a pre-existing Marzban user) —
    # never touched again after that (unlike usage_baseline, which rolls forward on
    # every settle). Exists solely so the monthly-average-usage estimate measures
    # usage actually OBSERVED by this dashboard, not a Marzban account's entire
    # pre-existing history misattributed to however many days it's been since sync
    # first saw it (see routers/accounts.py's enrich_accounts).
    first_seen_traffic: int = 0
    first_seen_traffic_at: Optional[datetime] = None

    # Marzban's own "last connected" timestamp, mirrored by the sync job —
    # used to derive whether this account is currently online (see
    # services.ONLINE_THRESHOLD_SECONDS) for the online-accounts trend chart.
    online_at: Optional[datetime] = None

    created_at: datetime = Field(default_factory=utcnow)


class LedgerEntry(SQLModel, table=True):
    """Append-only money ledger. Never update/delete a row to fix a balance —
    insert a correcting entry instead, so the audit trail stays intact."""

    id: Optional[int] = Field(default=None, primary_key=True)
    type: LedgerType
    amount: float
    date: datetime = Field(default_factory=utcnow)

    customer_id: Optional[int] = Field(default=None, foreign_key="customer.id")
    group_id: Optional[int] = Field(default=None, foreign_key="group.id")
    account_id: Optional[int] = Field(default=None, foreign_key="account.id")

    note: Optional[str] = None
    source: LedgerSource = LedgerSource.web


class AppSettings(SQLModel, table=True):
    """Single-row table (id is always 1) for dashboard-wide settings — currently
    just the default rate used when neither an account nor its group has one
    set. A real table (not a hardcoded default) so it's editable from the UI."""

    id: Optional[int] = Field(default=1, primary_key=True)
    default_rate_per_gb: Optional[float] = None


class AccountEvent(SQLModel, table=True):
    """Audit trail for direct Marzban actions (time/quota changes), since the
    field itself lives in Marzban and isn't duplicated here as an editable value."""

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="account.id")
    action: str  # "extend_expire" | "reduce_expire" | "set_data_limit" | "create"
    detail: str
    date: datetime = Field(default_factory=utcnow)
    source: LedgerSource = LedgerSource.web


class OnlineSnapshot(SQLModel, table=True):
    """One point in the online-accounts-count trend — written as a side effect
    of the regular sync job (every sync_interval_minutes), not a separate
    poller, since Marzban has no historical online-count endpoint of its own
    and a dedicated poller would mean extra Marzban logins/requests on top of
    the ones sync already makes. Trend granularity is therefore exactly the
    sync interval — documented, not silently assumed, in the reports router."""

    id: Optional[int] = Field(default=None, primary_key=True)
    recorded_at: datetime = Field(default_factory=utcnow, index=True)
    online_count: int
    total_accounts: int
