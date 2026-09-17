import asyncio
import functools
from collections import defaultdict
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app.models import Account, AppSettings, BillingMode, Customer, Group, LedgerEntry, LedgerSource, LedgerType, QueuedPlan, QueuedPlanStatus, utcnow

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


GbTotals = tuple[Optional[float], Optional[float], Optional[float], Optional[float]]
"""(gb_charged, gb_consumed, charged_amount, consumed_amount) for one scope:
GB billed / GB consumed / gross Toman billed / Toman value of the consumed
GB. Each component is None when no charge row in scope carries it — None
means "unknown", never zero (rows predating GB tracking, or money-only
manual entries, are excluded from the GB/derived sums but still count
toward charged_amount, which is plain money)."""


def _merge_opt(total: Optional[float], value: Optional[float]) -> Optional[float]:
    """None-propagating addition for optional money/GB figures: unknown +
    unknown stays unknown, but a known figure among unknowns is still worth
    showing."""
    if value is None:
        return total
    return value if total is None else total + value


def _merge_gb(a: GbTotals, b: GbTotals) -> GbTotals:
    return (
        _merge_opt(a[0], b[0]),
        _merge_opt(a[1], b[1]),
        _merge_opt(a[2], b[2]),
        _merge_opt(a[3], b[3]),
    )


class MoneyBook:
    """Answers "what does X owe right now" for accounts, groups and customers
    off a single consistent snapshot.

    Built once per request and passed around, so every figure on a page comes
    from the same read — two numbers on the same screen cannot disagree
    because one of them was computed a query later than the other.
    """

    def __init__(self, session: Session, *, since: Optional[datetime] = None):
        """`since`, when given, restricts every `*_posted` figure to
        LedgerEntry rows dated on or after it — "what do they owe FROM this
        date forward" (e.g. since their last payment), not all-time. Applied
        once here, at the SQL aggregation level, so every posted-balance
        method below (account/group/customer) automatically respects it
        without each needing its own date-filtering logic. `*_pending` is
        deliberately UNAFFECTED — it's a live "right now" figure read off
        Marzban's current usage snapshot, not ledger history, so "since" has
        no meaning for it. The same split applies to the GB totals: gb
        posted figures respect `since`, gb_pending does not."""
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
        # Sum by group_id where account_id is null and group_id is not null
        stmt_grp = select(LedgerEntry.group_id, LedgerEntry.type, func.sum(LedgerEntry.amount)).where(LedgerEntry.account_id.is_(None), LedgerEntry.group_id.is_not(None)).group_by(LedgerEntry.group_id, LedgerEntry.type)
        # Sum by customer_id where account_id is null and group_id is null and customer_id is not null
        stmt_cust = select(LedgerEntry.customer_id, LedgerEntry.type, func.sum(LedgerEntry.amount)).where(LedgerEntry.account_id.is_(None), LedgerEntry.group_id.is_(None), LedgerEntry.customer_id.is_not(None)).group_by(LedgerEntry.customer_id, LedgerEntry.type)
        if since is not None:
            stmt_acc = stmt_acc.where(LedgerEntry.date >= since)
            stmt_grp = stmt_grp.where(LedgerEntry.date >= since)
            stmt_cust = stmt_cust.where(LedgerEntry.date >= since)

        for acc_id, l_type, total in session.exec(stmt_acc).all():
            self._posted_by_account[acc_id] += total if l_type == LedgerType.charge else -total
        for grp_id, l_type, total in session.exec(stmt_grp).all():
            self._posted_group_only[grp_id] += total if l_type == LedgerType.charge else -total
        for cust_id, l_type, total in session.exec(stmt_cust).all():
            self._posted_customer_only[cust_id] += total if l_type == LedgerType.charge else -total

        # GB + gross-charge totals ride the same three buckets and the same
        # `since` filter as the money above — but only CHARGE rows (a payment
        # zeroes debt, it doesn't un-sell data), and NULL gb_amount /
        # consumed_gb (rows predating GB tracking, or money-only manual
        # entries) are EXCLUDED from the GB sums rather than counted as zero:
        # a window whose charges all predate the field must read "unknown",
        # not a lying 0. charged_amount is plain money — every charge counts
        # toward it even when its GB is unknown.
        self._gb_by_account: dict[int, GbTotals] = {}
        self._gb_group_only: dict[int, GbTotals] = {}
        self._gb_customer_only: dict[int, GbTotals] = {}
        gb_specs = (
            (
                select(
                    LedgerEntry.account_id,
                    func.sum(LedgerEntry.gb_amount),
                    func.sum(LedgerEntry.consumed_gb),
                    func.sum(LedgerEntry.amount),
                    func.sum(LedgerEntry.consumed_amount),
                )
                .where(LedgerEntry.account_id.is_not(None), LedgerEntry.type == LedgerType.charge)
                .group_by(LedgerEntry.account_id),
                self._gb_by_account,
            ),
            (
                select(
                    LedgerEntry.group_id,
                    func.sum(LedgerEntry.gb_amount),
                    func.sum(LedgerEntry.consumed_gb),
                    func.sum(LedgerEntry.amount),
                    func.sum(LedgerEntry.consumed_amount),
                )
                .where(LedgerEntry.account_id.is_(None), LedgerEntry.group_id.is_not(None), LedgerEntry.type == LedgerType.charge)
                .group_by(LedgerEntry.group_id),
                self._gb_group_only,
            ),
            (
                select(
                    LedgerEntry.customer_id,
                    func.sum(LedgerEntry.gb_amount),
                    func.sum(LedgerEntry.consumed_gb),
                    func.sum(LedgerEntry.amount),
                    func.sum(LedgerEntry.consumed_amount),
                )
                .where(LedgerEntry.account_id.is_(None), LedgerEntry.group_id.is_(None), LedgerEntry.customer_id.is_not(None), LedgerEntry.type == LedgerType.charge)
                .group_by(LedgerEntry.customer_id),
                self._gb_customer_only,
            ),
        )
        for stmt, bucket in gb_specs:
            if since is not None:
                stmt = stmt.where(LedgerEntry.date >= since)
            for scope_id, gb_c, gb_u, amt, amt_u in session.exec(stmt).all():
                bucket[scope_id] = (gb_c, gb_u, amt, amt_u)

        # How much of each account's current meter epoch its charges have
        # ALREADY attributed as consumed (see attributable_consumed_gb) —
        # accrued usage minus this is the live "in progress" figure
        # gb_pending reports. Entries dated at or before the epoch's start
        # belong to the previous epoch (closure charges are dated exactly at
        # it — every charge site stamps date=now and the same `now` becomes
        # the new usage_baseline_at) and are excluded by the strict compare.
        self._epoch_consumed: dict[int, float] = defaultdict(float)
        accounts_by_id = {a.id: a for a in self._accounts}
        consumed_rows = session.exec(
            select(LedgerEntry.account_id, LedgerEntry.consumed_gb, LedgerEntry.date)
            .where(LedgerEntry.account_id.is_not(None), LedgerEntry.consumed_gb.is_not(None))
        ).all()
        for acc_id, c_gb, entry_date in consumed_rows:
            account = accounts_by_id.get(acc_id)
            if account is None or c_gb is None:
                continue
            epoch_start = account.usage_baseline_at
            if epoch_start is not None and entry_date is not None:
                # Both round-trip through SQLite as naive — normalise anyway
                # so an in-session aware value can't raise on comparison.
                if epoch_start.tzinfo is not None:
                    epoch_start = epoch_start.replace(tzinfo=None)
                if entry_date.tzinfo is not None:
                    entry_date = entry_date.replace(tzinfo=None)
                if entry_date <= epoch_start:
                    continue
            self._epoch_consumed[acc_id] += c_gb

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

    def account_gb(self, account: Account) -> GbTotals:
        """(gb_charged, gb_consumed, charged_amount, consumed_amount) posted
        against this account — components are None when no charge row in
        scope carries them (see GbTotals)."""
        return self._gb_by_account.get(account.id, (None, None, None, None))

    def account_gb_pending(self, account: Account) -> float:
        """Usage accrued since the account's current meter epoch started that
        no charge has attributed yet — the GB sibling of account_pending
        (live, "right now", deliberately unaffected by `since`)."""
        accrued_gb = max(0, account.used_traffic - account.usage_baseline) / GB
        return round(max(0.0, accrued_gb - self._epoch_consumed.get(account.id, 0.0)), 3)

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

    def group_gb(self, group: Group) -> GbTotals:
        totals: GbTotals = (None, None, None, None)
        for a in self.group_members(group):
            totals = _merge_gb(totals, self._gb_by_account.get(a.id, (None, None, None, None)))
        totals = _merge_gb(totals, self._gb_group_only.get(group.id, (None, None, None, None)))
        return totals

    def group_gb_pending(self, group: Group) -> float:
        return round(sum(self.account_gb_pending(a) for a in self.group_members(group)), 3)

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

    def customer_gb(self, customer: Customer) -> GbTotals:
        """Same roll-up shape as customer_posted: directly-owned accounts +
        represented groups + customer-only entries — never both paths for a
        grouped account."""
        totals: GbTotals = (None, None, None, None)
        for a in self.customer_accounts(customer):
            totals = _merge_gb(totals, self._gb_by_account.get(a.id, (None, None, None, None)))
        for g in self.represented_groups(customer):
            totals = _merge_gb(totals, self.group_gb(g))
        totals = _merge_gb(totals, self._gb_customer_only.get(customer.id, (None, None, None, None)))
        return totals

    def customer_gb_pending(self, customer: Customer) -> float:
        total = sum(self.account_gb_pending(a) for a in self.customer_accounts(customer))
        total += sum(self.group_gb_pending(g) for g in self.represented_groups(customer))
        return round(total, 3)


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
    if not account.data_limit:  # None or Marzban's 0 == unlimited
        import logging
        logging.getLogger(__name__).warning("Prepay unlimited account %s (id=%s) requires manual invoicing. Returning 0.", account.marzban_username, account.id)
        return 0
    return max(0, account.data_limit - account.billed_data_limit)


def sync_marzban_fields(account: Account, marzban_user: dict) -> None:
    """Mirrors a Marzban API response (reset/modify/create) onto the local
    Account row — the 6 fields any such call can change. Shared by
    reset_account and every payg settle path (settle_account/settle_group/
    settle_group_member) so this stays in one place instead of drifting
    across copies.

    subscription_url is mirrored with `or`, never a plain assignment: the
    token in it exists only in Marzban and cannot be recomputed here, so a
    response that simply omits the field must leave the stored value alone
    rather than blanking the one copy this dashboard has."""
    account.used_traffic = marzban_user.get("used_traffic", 0)
    account.lifetime_used_traffic = marzban_user.get("lifetime_used_traffic", account.lifetime_used_traffic)
    account.expire = marzban_user.get("expire", account.expire)
    account.data_limit = marzban_user.get("data_limit", account.data_limit)
    account.status = marzban_user.get("status", account.status)
    account.subscription_url = marzban_user.get("subscription_url") or account.subscription_url


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


billing_lock = asyncio.Lock()


def serialise_billing(fn):
    """Runs the decorated endpoint one at a time, across the whole process.

    The settle/reset paths were written as SELECT ... with_for_update(), which
    is a silent no-op on SQLite: it parses, locks nothing, and reads exactly
    like protection that is not there. Two settles for the same account
    arriving together would each compute the amount from the same baseline
    and post two charges. This makes that impossible while the backend is one
    process — which the deployment is, and which the docstring above
    with_for_update's remaining uses says out loud.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        async with billing_lock:
            return await fn(*args, **kwargs)
    return wrapper


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


def attributable_consumed_gb(session: Session, account: Account) -> float:
    """The slice of this account's accrued usage (used_traffic - usage_baseline)
    that NO earlier charge has already attributed, in GB — the consumed_gb a
    charge posted RIGHT NOW should carry.

    Charges within one meter epoch (the span since usage_baseline was last
    set — an activation, a payg settle/reset, or a detected external reset)
    split the epoch's consumption between them without overlap: the first
    charge in the epoch attributes everything accrued so far, the next one
    only what accrued since. That's what makes the operator's "40 GB charged,
    33 GB consumed" come out right when two packages are billed once each at
    the end of their life — and keeps it right when one package is billed
    piecemeal (settle, then a top-up, then settling the top-up).

    Only meaningful for prepay-mode charge sites: payg's baseline rolls
    forward at every settle, so its accrued figure IS the cycle's whole
    consumption (sites there pass billable_gb directly). Call BEFORE any
    code path overwrites used_traffic/usage_baseline (sync_marzban_fields,
    activation resets) — those callers document that ordering locally."""
    from sqlmodel import func

    accrued_gb = max(0, account.used_traffic - account.usage_baseline) / GB
    stmt = (
        select(func.sum(LedgerEntry.consumed_gb))
        .where(
            LedgerEntry.account_id == account.id,
            LedgerEntry.consumed_gb.is_not(None),
        )
    )
    epoch_start = account.usage_baseline_at
    if epoch_start is not None:
        if epoch_start.tzinfo is not None:
            epoch_start = epoch_start.replace(tzinfo=None)
        # Strictly after: an entry dated exactly at the epoch start is that
        # epoch's own CLOSURE charge (every charge site stamps date=now and
        # the same `now` becomes the new usage_baseline_at), not a member of
        # this one.
        stmt = stmt.where(LedgerEntry.date > epoch_start)
    # Single-aggregate select: .one() yields the scalar directly (same shape
    # as ledger.py's func.max/func.count reads). SUM over an all-NULL or
    # empty set is None — meaning "nothing attributed yet", i.e. zero.
    recorded_total = session.exec(stmt).one() or 0.0
    return round(max(0.0, accrued_gb - recorded_total), 3)


def close_out_payg_usage_before_delete(
    session: Session, account: Account, *, source: LedgerSource, note: str,
    created_by: Optional[str] = None,
) -> Optional[LedgerEntry]:
    """Before permanently removing an account, any UNBILLED payg usage since
    the last settle is a real meter reading that only exists locally —
    deleting the Marzban user without billing it first loses it forever
    (unlike prepay, where the package size is already fixed data, not a
    live reading). Returns the entry to add (caller decides when to
    session.add it — see the "Marzban call before any DB write" ordering
    every deletion here follows), or None if there's nothing owed (prepay,
    or zero accrued usage).

    Also rolls usage_baseline forward when it posts a charge — skipping
    that would leave MoneyBook.account_pending reading the SAME
    already-billed usage as still pending forever, since nothing else ever
    revisits a deleted account's baseline (it's excluded from every
    "operator can still act on this" screen but MoneyBook itself
    deliberately keeps summing deleted accounts' money — see
    Account.deleted_at's own docstring)."""
    mode = effective_billing_mode(session, account)
    if mode != BillingMode.payg:
        return None
    billable_gb = billable_bytes(account, mode) / GB
    rate = effective_rate(session, account)
    amount = round(billable_gb * rate, 2)
    if amount <= 0:
        return None
    entry = LedgerEntry(
        type=LedgerType.charge,
        amount=amount,
        customer_id=account.customer_id,
        group_id=account.group_id,
        account_id=account.id,
        note=note,
        source=source,
        # Payg close-out: the bill IS the meter's final reading — both GB
        # figures are the same here, and the baseline roll below starts a
        # fresh epoch so nothing double-counts later.
        gb_amount=round(billable_gb, 3),
        consumed_gb=round(billable_gb, 3),
        consumed_amount=amount,
        created_by=created_by,
    )
    account.usage_baseline = account.used_traffic
    account.usage_baseline_at = utcnow()
    return entry


def cancel_pending_queued_plan(session: Session, account_id: int) -> None:
    """A pending QueuedPlan (auto-queued by sync_job, or set by hand) left
    behind after an account is deleted would sit 'pending' forever —
    sync_job only ever activates one while iterating Marzban's live user
    list, and a deleted account has left that list for good — so it would
    keep showing as a phantom future charge on
    /api/reports/upcoming-renewals for an account that no longer exists."""
    pending_plan = session.exec(
        select(QueuedPlan).where(QueuedPlan.account_id == account_id, QueuedPlan.status == QueuedPlanStatus.pending)
    ).first()
    if pending_plan is not None:
        pending_plan.status = QueuedPlanStatus.cancelled
        session.add(pending_plan)


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
# Below this much time on the CURRENT cycle, a per-cycle pace is noise (an
# hour of downloading would extrapolate to absurd monthly figures) — the
# estimator falls back to the lifetime average until the cycle is this old.
MIN_CYCLE_PACE_DAYS = 0.5
# Below a full billing month of observed history, still show a number (it's
# useful) but flag it as preliminary so the UI can visually distinguish it from
# a settled figure.
FULL_CONFIDENCE_DAYS = 30.0
# The standard shape for a pay-as-you-go account (applied by update_billing
# when an account switches to payg): no expiry to ever "end the plan", and a
# soft cap big enough that the cap-hit billing rhythm stays occasional rather
# than constant — the cap-hit flow bills accrued usage and keeps the account
# alive, which IS payg's normal cycle.
PAYG_DEFAULT_DATA_LIMIT_GB = 300.0


def monthly_avg_usage(account: Account, now: datetime) -> tuple[Optional[float], str, float]:
    """Estimated monthly usage rate for this account — what the auto-queue
    sizes the next plan from, and the "Monthly average" figure the dashboard
    shows (one number, one source of truth, so they can never drift).

    Prefers the CURRENT cycle's observed pace — (used_traffic −
    usage_baseline) over the time since usage_baseline_at — once that cycle
    has MIN_CYCLE_PACE_DAYS of observation. The lifetime average this used
    to be is still the fallback, but it alone had a real blind spot: a user
    who sits idle for weeks and then starts burning 2GB a day keeps showing
    their idle-era average (2.7 GB/mo) while actually running at ~60 GB/mo,
    so auto-queue sizes their next plan at a fraction of their real demand
    and they churn through plans. Usage is what the account is doing NOW.

    Falls back to the lifetime average (observed since first_seen_traffic_at
    — see Account.first_seen_traffic) when: the cycle is unobserved
    (usage_baseline_at missing on legacy rows), younger than
    MIN_CYCLE_PACE_DAYS (a half-day extrapolation is noise), or the user
    hasn't used anything this cycle yet (a zero pace would read as "dead"
    rather than "idle" — the lifetime figure is the honest statement then).

    Returns (monthly_avg_usage_gb, usage_confidence, observed_days) where
    observed_days is the window the returned figure was computed over.
    monthly_avg_usage_gb is None below MIN_USAGE_SAMPLE_DAYS of observed
    history — too little data for a trustworthy monthly rate, not a number
    to guess. `now` must be naive (see callers: this compares directly
    against usage_baseline_at/first_seen_traffic_at/created_at, which
    round-trip through SQLite as naive)."""
    # Current-cycle pace: what the account is actually doing now.
    if account.usage_baseline_at is not None:
        cycle_days = (now - account.usage_baseline_at).total_seconds() / 86400
        cycle_bytes = max(0, account.used_traffic - account.usage_baseline)
        if cycle_days >= MIN_CYCLE_PACE_DAYS and cycle_bytes > 0:
            cycle_avg_gb = round((cycle_bytes / GB) / cycle_days * 30, 2)
            usage_confidence = "full" if cycle_days >= FULL_CONFIDENCE_DAYS else "preliminary"
            return cycle_avg_gb, usage_confidence, cycle_days

    # Lifetime fallback: the whole observed history since first sight.
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
