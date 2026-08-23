from collections import defaultdict
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app.models import Account, AppSettings, BillingMode, Customer, Group, LedgerEntry, LedgerType, QueuedPlan, QueuedPlanStatus, utcnow

# ══════════════════════════════════════════════════════════════ the money model
#
# ONE OWNER PER ENTRY. Every LedgerEntry is owned by exactly one scope,
# resolved in this order:
#
#     account_id set     -> that account's own money
#     else group_id set  -> group-level money not tied to any one member
#     else customer_id   -> customer-level money not tied to any account
#
# The other FK columns are still written and still drive the ledger/history
# views, but they are NEVER summed into a balance. An entry counted at two
# levels at once is precisely how one member's payment could flip their whole
# group into being a creditor: the payment landed in the group's pooled total
# while the matching charge sat somewhere else, so the two never cancelled.
#
# ROLL-UPS ARE SUMS OF THE LEVEL BELOW — never an independent re-query that
# could overlap, miss, or drift:
#
#     account   = its own entries            (+ its uninvoiced usage, for net)
#     group     = Σ members + group-only entries
#     customer  = Σ directly-owned accounts + Σ represented groups
#                 + customer-only entries
#
# Because each level is defined as the sum of the level below it, a group's
# figure can never disagree with the member rows printed underneath it. That
# consistency is the whole point — it is a structural guarantee, not something
# each screen has to remember to reproduce.
#
# Two figures exist at every level and are always shown NETTED, never side by
# side (see AccountRow.net_owed):
#     posted  — already invoiced (real ledger entries)
#     pending — accrued but not yet invoiced
#     net     = posted + pending  ("owes now")
# Settling moves an amount from pending to posted, leaving net unchanged,
# which is correct: formalising a bill doesn't change what someone owes.


def _signed(entry: LedgerEntry) -> float:
    """Charges are positive (they owe us), credits negative (we owe them)."""
    return entry.amount if entry.type == LedgerType.charge else -entry.amount


class MoneyBook:
    """Answers "what does X owe right now" for accounts, groups and customers
    off a single consistent snapshot.

    Built once per request and passed around, so every figure on a page comes
    from the same read — two numbers on the same screen cannot disagree
    because one of them was computed a query later than the other.
    """

    def __init__(self, session: Session):
        self.session = session
        self._accounts = session.exec(select(Account)).all()
        self._groups = {g.id: g for g in session.exec(select(Group)).all()}

        # Each entry bucketed exactly once, by its single owning scope.
        self._posted_by_account: dict[int, float] = defaultdict(float)
        self._posted_group_only: dict[int, float] = defaultdict(float)
        self._posted_customer_only: dict[int, float] = defaultdict(float)
        from sqlmodel import func

        # Sum by account_id where account_id is not null
        stmt_acc = select(LedgerEntry.account_id, LedgerEntry.type, func.sum(LedgerEntry.amount)).where(LedgerEntry.account_id.is_not(None)).group_by(LedgerEntry.account_id, LedgerEntry.type)
        for acc_id, l_type, total in session.exec(stmt_acc).all():
            self._posted_by_account[acc_id] += total if l_type == LedgerType.charge else -total

        # Sum by group_id where account_id is null and group_id is not null
        stmt_grp = select(LedgerEntry.group_id, LedgerEntry.type, func.sum(LedgerEntry.amount)).where(LedgerEntry.account_id.is_(None), LedgerEntry.group_id.is_not(None)).group_by(LedgerEntry.group_id, LedgerEntry.type)
        for grp_id, l_type, total in session.exec(stmt_grp).all():
            self._posted_group_only[grp_id] += total if l_type == LedgerType.charge else -total

        # Sum by customer_id where account_id is null and group_id is null and customer_id is not null
        stmt_cust = select(LedgerEntry.customer_id, LedgerEntry.type, func.sum(LedgerEntry.amount)).where(LedgerEntry.account_id.is_(None), LedgerEntry.group_id.is_(None), LedgerEntry.customer_id.is_not(None)).group_by(LedgerEntry.customer_id, LedgerEntry.type)
        for cust_id, l_type, total in session.exec(stmt_cust).all():
            self._posted_customer_only[cust_id] += total if l_type == LedgerType.charge else -total

        self._members: dict[int, list[Account]] = defaultdict(list)
        self._owned_directly: dict[int, list[Account]] = defaultdict(list)
        for a in self._accounts:
            if a.group_id is not None:
                self._members[a.group_id].append(a)
            elif a.customer_id is not None:
                # Grouped accounts roll up through their GROUP (which rolls up
                # to its representative), never also through their own
                # customer — that would be the same double count again.
                self._owned_directly[a.customer_id].append(a)

        self._pending_cache: dict[int, float] = {}

    # ------------------------------------------------------------- accounts
    def account_posted(self, account: Account) -> float:
        return self._posted_by_account.get(account.id, 0.0)

    def account_pending(self, account: Account) -> float:
        """Accrued but not yet invoiced, at this account's effective rate."""
        if account.id not in self._pending_cache:
            group = self._groups.get(account.group_id) if account.group_id else None
            mode = effective_billing_mode(self.session, account, group)
            billable_gb = billable_bytes(account, mode) / GB
            self._pending_cache[account.id] = round(billable_gb * effective_rate(self.session, account, group), 2)
        return self._pending_cache[account.id]

    def account_net(self, account: Account) -> float:
        return round(self.account_posted(account) + self.account_pending(account), 2)

    # --------------------------------------------------------------- groups
    def group_members(self, group: Group) -> list[Account]:
        return self._members.get(group.id, [])

    def group_posted(self, group: Group) -> float:
        members = sum(self.account_posted(a) for a in self.group_members(group))
        return round(members + self._posted_group_only.get(group.id, 0.0), 2)

    def group_pending(self, group: Group) -> float:
        return round(sum(self.account_pending(a) for a in self.group_members(group)), 2)

    def group_net(self, group: Group) -> float:
        return round(self.group_posted(group) + self.group_pending(group), 2)

    # ------------------------------------------------------------ customers
    def customer_accounts(self, customer: Customer) -> list[Account]:
        """Accounts this customer pays for directly (not via a group)."""
        return self._owned_directly.get(customer.id, [])

    def represented_groups(self, customer: Customer) -> list[Group]:
        return [g for g in self._groups.values() if g.representative_customer_id == customer.id]

    def customer_posted(self, customer: Customer) -> float:
        total = sum(self.account_posted(a) for a in self.customer_accounts(customer))
        total += sum(self.group_posted(g) for g in self.represented_groups(customer))
        total += self._posted_customer_only.get(customer.id, 0.0)
        return round(total, 2)

    def customer_pending(self, customer: Customer) -> float:
        total = sum(self.account_pending(a) for a in self.customer_accounts(customer))
        total += sum(self.group_pending(g) for g in self.represented_groups(customer))
        return round(total, 2)

    def customer_net(self, customer: Customer) -> float:
        return round(self.customer_posted(customer) + self.customer_pending(customer), 2)


def account_posted_balance(session: Session, account_id: int) -> float:
    """One account's posted balance, read directly — for the settle endpoints,
    which need this mid-transaction and shouldn't pay for a whole MoneyBook."""
    from sqlmodel import func
    stmt = select(LedgerEntry.type, func.sum(LedgerEntry.amount)).where(LedgerEntry.account_id == account_id).group_by(LedgerEntry.type)
    return sum(amount if l_type == LedgerType.charge else -amount for l_type, amount in session.exec(stmt).all())


def group_only_posted_balance(session: Session, group_id: int) -> float:
    """The part of a group's posted balance that belongs to the group itself
    rather than to any one member (a setup fee, an adjustment). Settling a
    group and marking it paid has to clear this too, or "paid in full" would
    leave the group still owing money it had no member to attribute it to."""
    from sqlmodel import func
    stmt = select(LedgerEntry.type, func.sum(LedgerEntry.amount)).where(LedgerEntry.group_id == group_id, LedgerEntry.account_id.is_(None)).group_by(LedgerEntry.type)
    return sum(amount if l_type == LedgerType.charge else -amount for l_type, amount in session.exec(stmt).all())


GB = 1024**3


def bytes_from_gb(gb: float) -> int:
    return round(gb * GB)


def billable_bytes(account: Account, mode: BillingMode) -> int:
    """payg bills what was actually USED since the last settle (metered:
    used_traffic - usage_baseline). prepay bills the PACKAGE SIZE itself
    since the last settle (data_limit - billed_data_limit) — "prepay" means
    paying for what was sold up front, not for what's been consumed out of
    it; a customer who bought a 42GB package owes for 42GB the moment it's
    sold, not only for whatever fraction of it they've used so far. An
    unlimited (data_limit=None) prepay package has no fixed size to bill
    automatically — invoice it manually instead."""
    if mode == BillingMode.payg:
        return max(0, account.used_traffic - account.usage_baseline)
    if account.data_limit is None:
        import logging
        logging.getLogger(__name__).warning("Prepay unlimited account %s (id=%s) requires manual invoicing. Returning 0.", account.marzban_username, account.id)
        return 0
    return max(0, account.data_limit - account.billed_data_limit)


def sync_marzban_fields(account: Account, marzban_user: dict) -> None:
    """Mirrors a Marzban API response (reset/modify/create) onto the local
    Account row — the 5 fields any such call can change. Shared by
    reset_account and every payg settle path (settle_account/settle_group/
    settle_group_member) so this stays in one place instead of drifting
    across copies."""
    account.used_traffic = marzban_user.get("used_traffic", 0)
    account.lifetime_used_traffic = marzban_user.get("lifetime_used_traffic", account.lifetime_used_traffic)
    account.expire = marzban_user.get("expire", account.expire)
    account.data_limit = marzban_user.get("data_limit", account.data_limit)
    account.status = marzban_user.get("status", account.status)


def roll_payg_baseline_after_reset(account: Account, now: datetime) -> None:
    """After Marzban usage has actually been reset (used_traffic is now the
    post-reset value — normally 0), the payg billing baseline must roll to
    match, or the next settle would either bill the same usage twice, or —
    if the post-reset value landed below the old baseline — silently bill
    nothing until usage climbs back past it. Call this AFTER
    sync_marzban_fields, never before (it reads the just-synced
    used_traffic)."""
    account.usage_baseline = account.used_traffic
    account.usage_baseline_at = now


def get_settings(session: Session) -> AppSettings:
    settings = session.get(AppSettings, 1)
    if settings is None:
        settings = AppSettings(id=1)
        session.add(settings)
        session.commit()
        session.refresh(settings)
    return settings


def get_default_rate(session: Session) -> float:
    return get_settings(session).default_rate_per_gb or 0


def effective_rate(session: Session, account: Account, group: Optional[Group] = None) -> float:
    """account's own rate wins, then its group's rate, then the dashboard-wide
    default — the same fallback chain used everywhere billing math touches a
    rate, so "set a global rate" (the operator's request) actually reaches
    every calculation instead of only the ones someone remembered to update."""
    if account.rate_per_gb is not None:
        return account.rate_per_gb
    if group is None and account.group_id is not None:
        group = session.get(Group, account.group_id)
    if group is not None and group.rate_per_gb is not None:
        return group.rate_per_gb
    return get_default_rate(session)


def effective_billing_mode(session: Session, account: Account, group: Optional[Group] = None) -> BillingMode:
    """A grouped account's OWN billing_mode field is close to vestigial: group
    settle/reset-cycle already bills every member by the GROUP's mode
    regardless of it (see routers/groups.py's _invoice_lines, which never
    checks a member's billing_mode). But the field defaults to 'prepay' and
    nothing ever syncs it to match the group when an account is assigned — so
    a member of a payg group whose own field was simply never touched still
    reads as 'prepay' everywhere that checks the raw field instead of the
    group, contradicting how it's actually billed. The group's mode always
    wins for a grouped account; the account's own field only matters once
    it's standalone."""
    if account.group_id is not None:
        if group is None:
            group = session.get(Group, account.group_id)
        if group is not None:
            return group.billing_mode
    return account.billing_mode


def rate_is_configured(session: Session, account: Account, group: Optional[Group] = None) -> bool:
    """Whether something in the chain was actually SET, as opposed to what it
    resolves TO. effective_rate() alone can't distinguish "nobody has ever set
    a rate anywhere" from "an operator explicitly priced this account at 0 for
    a comp/free account" — both resolve to 0, but only the first one should be
    flagged as a misconfiguration."""
    if account.rate_per_gb is not None:
        return True
    if group is None and account.group_id is not None:
        group = session.get(Group, account.group_id)
    if group is not None and group.rate_per_gb is not None:
        return True
    return get_settings(session).default_rate_per_gb is not None


# Below this many days of *observed* usage, a "monthly average" would be
# extrapolated from too little data to be trustworthy (a 6-hour-old observation
# window with 2GB used does NOT mean "144GB/month") — report insufficient_data
# instead of a number.
MIN_USAGE_SAMPLE_DAYS = 3.0
# Below a full billing month of observed history, still show a number (it's
# useful) but flag it as preliminary so the UI can visually distinguish it from
# a settled figure.
FULL_CONFIDENCE_DAYS = 30.0


def monthly_avg_usage(account: Account, now: datetime) -> tuple[Optional[float], str, float]:
    """Estimated monthly usage rate from lifetime traffic observed since this
    dashboard first saw the account (see Account.first_seen_traffic) —
    extracted out of enrich_accounts so anything else that needs "how much
    does this account typically use a month" (e.g. sizing an auto-queued
    next plan) reads the exact same number the dashboard shows, not a
    second, separately-computed one that could drift from it.

    Returns (monthly_avg_usage_gb, usage_confidence, observed_days).
    monthly_avg_usage_gb is None below MIN_USAGE_SAMPLE_DAYS of observed
    history — too little data for a trustworthy monthly rate, not a number
    to guess. `now` must be naive (see callers: this compares directly
    against first_seen_traffic_at/created_at, which round-trip through
    SQLite as naive)."""
    observed_since = account.first_seen_traffic_at or account.created_at
    observed_days = (now - observed_since).total_seconds() / 86400
    # max(0, ...): never negative, even if Marzban's lifetime counter were
    # ever reset below the captured baseline (it's meant to be monotonic,
    # but this keeps a platform anomaly from producing a negative rate).
    observed_bytes = max(0, account.lifetime_used_traffic - account.first_seen_traffic)

    if observed_days < MIN_USAGE_SAMPLE_DAYS:
        return None, "insufficient_data", observed_days
    monthly_avg_usage_gb = round((observed_bytes / GB) / observed_days * 30, 2)
    usage_confidence = "full" if observed_days >= FULL_CONFIDENCE_DAYS else "preliminary"
    return monthly_avg_usage_gb, usage_confidence, observed_days


def enrich_accounts(session: Session, accounts: list[Account], book: Optional[MoneyBook] = None) -> list:
    """Builds the AccountRow shape (balance, effective rate, monthly-average
    usage, etc.) shared by every endpoint that lists accounts — accounts.py's
    own list/detail routes, and customers.py/groups.py's account sub-lists —
    so all of them agree on the same resolved numbers instead of each screen
    computing (or failing to compute) its own version.

    Pass an existing `book` when the caller already built one (a group page
    also needs the group's own totals), so the rows and the header total come
    from the same snapshot."""
    from app.schemas import AccountRead, AccountRow  # local import: schemas imports nothing from here, avoids a cycle

    book = book or MoneyBook(session)
    customer_ids = {a.customer_id for a in accounts if a.customer_id}
    group_ids = {a.group_id for a in accounts if a.group_id}
    account_ids = {a.id for a in accounts if a.id is not None}
    
    customers = {c.id: c for c in session.exec(select(Customer).where(Customer.id.in_(customer_ids))).all()} if customer_ids else {}
    groups = {g.id: g for g in session.exec(select(Group).where(Group.id.in_(group_ids))).all()} if group_ids else {}
    # Batch-load pending QueuedPlans for all accounts at once (avoids N+1).
    accounts_with_next_plan: set[int] = set()
    if account_ids:
        pending_plans = session.exec(
            select(QueuedPlan.account_id).where(
                QueuedPlan.account_id.in_(account_ids),
                QueuedPlan.status == QueuedPlanStatus.pending,
            )
        ).all()
        accounts_with_next_plan = set(pending_plans)

    # created_at/first_seen_traffic_at round-trip through SQLite as naive even
    # though utcnow() produces an aware datetime (same quirk documented in
    # reports.py) — strip tzinfo here too so the subtraction below doesn't raise.
    now = utcnow().replace(tzinfo=None)
    rows = []
    for a in accounts:
        monthly_avg_usage_gb, usage_confidence, observed_days = monthly_avg_usage(a, now)

        customer = customers.get(a.customer_id) if a.customer_id else None
        group = groups.get(a.group_id) if a.group_id else None

        eff_mode = effective_billing_mode(session, a, group)
        # All three come from the one MoneyBook snapshot — see its docstring
        # for why posted/pending/net are defined the way they are, and why
        # every screen must read them from here rather than re-deriving.
        pending = book.account_pending(a)
        balance = round(book.account_posted(a), 2)

        rows.append(
            AccountRow(
                **AccountRead.model_validate(a, from_attributes=True).model_dump(),
                customer_name=customer.name if customer else None,
                group_name=group.name if group else None,
                effective_rate=effective_rate(session, a, group),
                rate_configured=rate_is_configured(session, a, group),
                payer_balance=balance,
                pending_amount=pending,
                # Posted debt and unbilled usage are the same debt at two
                # stages, not two separate debts — a payment already made
                # must count against usage not yet invoiced.
                net_owed=round(balance + pending, 2),
                effective_billing_mode=eff_mode,
                monthly_avg_usage_gb=monthly_avg_usage_gb,
                usage_confidence=usage_confidence,
                usage_sample_days=round(observed_days, 1),
                has_next_plan=a.id in accounts_with_next_plan,
            )
        )
    return rows
