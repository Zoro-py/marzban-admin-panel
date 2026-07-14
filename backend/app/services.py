from typing import Optional

from sqlmodel import Session, select

from app.models import Account, AppSettings, Customer, Group, LedgerEntry, LedgerType, utcnow


def compute_balance(session: Session, *, customer_id: Optional[int] = None, group_id: Optional[int] = None) -> tuple[float, float]:
    """Returns (total_charge, total_credit) for a customer or a group."""
    if (customer_id is None) == (group_id is None):
        raise ValueError("compute_balance needs exactly one of customer_id/group_id")

    stmt = select(LedgerEntry)
    stmt = stmt.where(LedgerEntry.customer_id == customer_id) if customer_id is not None else stmt.where(
        LedgerEntry.group_id == group_id
    )
    entries = session.exec(stmt).all()

    total_charge = sum(e.amount for e in entries if e.type == LedgerType.charge)
    total_credit = sum(e.amount for e in entries if e.type == LedgerType.credit)
    return total_charge, total_credit


GB = 1024**3


def bytes_from_gb(gb: float) -> int:
    return round(gb * GB)


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


def enrich_accounts(session: Session, accounts: list[Account]) -> list:
    """Builds the AccountRow shape (balance, effective rate, monthly-average
    usage, etc.) shared by every endpoint that lists accounts — accounts.py's
    own list/detail routes, and customers.py/groups.py's account sub-lists —
    so all of them agree on the same resolved numbers instead of each screen
    computing (or failing to compute) its own version."""
    from app.schemas import AccountRead, AccountRow  # local import: schemas imports nothing from here, avoids a cycle

    customers = {c.id: c for c in session.exec(select(Customer)).all()}
    groups = {g.id: g for g in session.exec(select(Group)).all()}

    # Cache balances per payer (customer_id or group_id) so N accounts sharing
    # one customer/group only compute that balance once, not N times.
    customer_balance_cache: dict[int, float] = {}
    group_balance_cache: dict[int, float] = {}

    def payer_balance(a: Account) -> float:
        if a.group_id is not None:
            if a.group_id not in group_balance_cache:
                charge, credit = compute_balance(session, group_id=a.group_id)
                group_balance_cache[a.group_id] = charge - credit
            return group_balance_cache[a.group_id]
        if a.customer_id is not None:
            if a.customer_id not in customer_balance_cache:
                charge, credit = compute_balance(session, customer_id=a.customer_id)
                customer_balance_cache[a.customer_id] = charge - credit
            return customer_balance_cache[a.customer_id]
        return 0.0

    # created_at/first_seen_traffic_at round-trip through SQLite as naive even
    # though utcnow() produces an aware datetime (same quirk documented in
    # reports.py) — strip tzinfo here too so the subtraction below doesn't raise.
    now = utcnow().replace(tzinfo=None)
    rows = []
    for a in accounts:
        # first_seen_traffic_at is when THIS dashboard first observed the account
        # (dashboard-create, or sync discovering a pre-existing Marzban user) —
        # created_at is only a fallback for rows from before this column existed
        # that somehow slipped past db.py's migration backfill.
        observed_since = a.first_seen_traffic_at or a.created_at
        observed_days = (now - observed_since).total_seconds() / 86400
        # max(0, ...): never negative, even if Marzban's lifetime counter were
        # ever reset below the captured baseline (it's meant to be monotonic,
        # but this keeps a platform anomaly from producing a negative rate).
        observed_bytes = max(0, a.lifetime_used_traffic - a.first_seen_traffic)

        if observed_days < MIN_USAGE_SAMPLE_DAYS:
            monthly_avg_usage_gb = None
            usage_confidence = "insufficient_data"
        else:
            monthly_avg_usage_gb = round((observed_bytes / (1024**3)) / observed_days * 30, 2)
            usage_confidence = "full" if observed_days >= FULL_CONFIDENCE_DAYS else "preliminary"

        customer = customers.get(a.customer_id) if a.customer_id else None
        group = groups.get(a.group_id) if a.group_id else None

        rows.append(
            AccountRow(
                **AccountRead.model_validate(a, from_attributes=True).model_dump(),
                customer_name=customer.name if customer else None,
                group_name=group.name if group else None,
                effective_rate=effective_rate(session, a, group),
                rate_configured=rate_is_configured(session, a, group),
                payer_balance=round(payer_balance(a), 2),
                monthly_avg_usage_gb=monthly_avg_usage_gb,
                usage_confidence=usage_confidence,
                usage_sample_days=round(observed_days, 1),
            )
        )
    return rows
