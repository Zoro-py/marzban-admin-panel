"""Charge history — a READ-ONLY windowed view of per-account charges.

Every query here is a SELECT; nothing in this module writes to any table,
touches Marzban, or produces side effects of any kind. Sums are computed
directly from the ledger rows inside the requested window — deliberately NOT
via services.MoneyBook, whose roll-ups answer "what does this entity owe
right now" (level-scoped, window-independent), while this endpoint answers
"what was charged/credited for these specific accounts during this window".
A group-level ledger row (no account_id) therefore never appears here, and a
deleted account's history still does (LedgerEntry rows outlive the account —
the append-only ledger is the point).

`gb_amount` NULL is "unknown", never zero: aggregates carry
charged_gb_known (sum of known-GB charges only, None when there are none)
plus charged_gb_known_count so the UI can say "GB recorded on X of Y
charges" instead of quietly treating legacy rows as 0 GB.

Timeline conventions copied from GET /api/ledger/balance: date-only inputs
are accepted at face value ("since the 1st" = entries from midnight that day;
"until the 23rd" = through the END of the 23rd), and timezone-aware inputs
are normalized to naive UTC before touching the stored rows — SQLite binds a
datetime's wall-clock fields verbatim, so a non-UTC offset would otherwise
silently compare as that offset's wall time against UTC-stored rows.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.auth import require_auth
from app.db import get_session
from app.models import (
    Account,
    AccountEvent,
    Customer,
    Group,
    LedgerEntry,
    LedgerType,
    QueuedPlan,
    QueuedPlanStatus,
)
from app.schemas import (
    ChargeHistoryResponse,
    HistoryAccount,
    HistoryEntry,
    HistoryMarker,
    HistoryPackage,
    HistorySummary,
)

router = APIRouter(prefix="/api/history", tags=["history"], dependencies=[Depends(require_auth)])

MAX_ACCOUNTS = 50
DEFAULT_WINDOW_DAYS = 180


@router.get("/accounts", response_model=list[HistoryAccount])
def history_accounts(session: Session = Depends(get_session)):
    """Every account, INCLUDING soft-deleted ones — the history view's picker
    needs deleted accounts selectable because their ledger history outlives
    them (append-only ledger), while GET /api/accounts is deliberately the
    live fleet view and filters deleted_at out. Read-only and unpaginated
    (same reasoning as that endpoint's unbounded list)."""
    accounts = session.exec(select(Account).order_by(Account.marzban_username)).all()
    customer_ids = {a.customer_id for a in accounts if a.customer_id is not None}
    group_ids = {a.group_id for a in accounts if a.group_id is not None}
    customer_names = (
        {c.id: c.name for c in session.exec(select(Customer).where(Customer.id.in_(customer_ids))).all()}  # type: ignore[arg-type]
        if customer_ids
        else {}
    )
    group_names = (
        {g.id: g.name for g in session.exec(select(Group).where(Group.id.in_(group_ids))).all()}  # type: ignore[arg-type]
        if group_ids
        else {}
    )
    return [
        HistoryAccount(
            id=a.id,
            username=a.marzban_username,
            status=a.status,
            deleted=a.deleted_at is not None,
            customer_id=a.customer_id,
            customer_name=customer_names.get(a.customer_id) if a.customer_id is not None else None,
            group_id=a.group_id,
            group_name=group_names.get(a.group_id) if a.group_id is not None else None,
            billing_mode=a.billing_mode,
        )
        for a in accounts
    ]

# AccountEvent actions shown as timeline ticks — the non-money "what happened"
# subset. Anything else (create, extend_expire, …) stays in the per-account
# inspector; this view is about charges and volume changes.
MARKER_ACTIONS = (
    "external_data_limit_increase",
    "adjust",
    "external_usage_reset",
    "payg_cap_hit_reset",
    "settle_reset",
    "deleted_from_marzban",
)


def _to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _parse_account_ids(raw: str) -> list[int]:
    """"3,46,57" -> [3, 46, 57], deduplicated preserving order.

    Malformed input is a client bug worth a clear 400 (not a bare 422): a
    copied URL with "a=3,,46" or "a=3;46" should say exactly what's wrong.
    """
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        raise HTTPException(400, "account_ids is required — a comma-separated list of account ids")
    ids: list[int] = []
    for token in tokens:
        # ASCII digits only — "۳" is technically int()-able but accepting it
        # would make error messages and logs ambiguous; same convention as the
        # bot's ASCII-only digit handling.
        if not (token.isascii() and token.isdigit()):
            raise HTTPException(400, f"account_ids must be comma-separated integers, got {token!r}")
        value = int(token)
        if value not in ids:
            ids.append(value)
    if len(ids) > MAX_ACCOUNTS:
        raise HTTPException(400, f"Too many accounts: {len(ids)} requested, maximum is {MAX_ACCOUNTS}")
    return ids


def _summarize(rows: list[LedgerEntry]) -> HistorySummary:
    """Aggregate one window's rows for one scope. ALWAYS computed from the
    full row set — include_credits=false filters the returned `entries` list
    only, never these sums (hiding credits from the table must not pretend
    they weren't received)."""
    charges = [r for r in rows if r.type == LedgerType.charge]
    credits = [r for r in rows if r.type == LedgerType.credit]
    known_gb = [r.gb_amount for r in charges if r.gb_amount is not None]
    first = min((r.date for r in charges), default=None)
    last = max((r.date for r in charges), default=None)
    avg_gap = None
    if len(charges) >= 2 and first is not None and last is not None:
        avg_gap = round((last - first).total_seconds() / 86400 / (len(charges) - 1), 1)
    return HistorySummary(
        charge_count=len(charges),
        charged_amount=round(sum(r.amount for r in charges), 2),
        charged_gb_known=round(sum(known_gb), 3) if known_gb else None,
        charged_gb_known_count=len(known_gb),
        credit_count=len(credits),
        credited_amount=round(sum(r.amount for r in credits), 2) if credits else None,
        first_charge_at=first,
        last_charge_at=last,
        avg_days_between_charges=avg_gap,
    )


@router.get("/charges", response_model=ChargeHistoryResponse)
def charge_history(
    account_ids: str = Query(..., description="Comma-separated account ids, e.g. 3,46,57"),
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    include_credits: bool = False,
    session: Session = Depends(get_session),
):
    ids = _parse_account_ids(account_ids)

    # A date-only `until` covers its whole day; a timestamp is used as given.
    # Default window: the recent DEFAULT_WINDOW_DAYS, so an operator landing
    # on the page with no explicit range sees something sensible.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if until is None:
        until_naive = now
    else:
        until_naive = _to_naive_utc(until)
        if until_naive.time() == datetime.min.time():
            until_naive = until_naive + timedelta(days=1) - timedelta(microseconds=1)
    if since is None:
        since_naive = now - timedelta(days=DEFAULT_WINDOW_DAYS)
    else:
        since_naive = _to_naive_utc(since)
    if since_naive > until_naive:
        raise HTTPException(400, "since must not be after until")

    accounts = session.exec(select(Account).where(Account.id.in_(ids))).all()  # type: ignore[arg-type]
    found_ids = {a.id for a in accounts}
    missing = [i for i in ids if i not in found_ids]
    if missing:
        raise HTTPException(404, f"account ids not found: {', '.join(str(m) for m in missing)}")
    # Emit in the REQUESTED order — the frontend lanes keep the picker's order
    # instead of whatever the DB happened to return.
    by_id = {a.id: a for a in accounts}
    ordered_accounts = [by_id[i] for i in ids]

    # One IN-query per table — never a per-account loop.
    rows = session.exec(
        select(LedgerEntry)
        .where(LedgerEntry.account_id.in_(ids))  # type: ignore[arg-type]
        .where(LedgerEntry.date >= since_naive)
        .where(LedgerEntry.date <= until_naive)
        .order_by(LedgerEntry.date, LedgerEntry.id)
    ).all()
    packages = session.exec(
        select(QueuedPlan)
        .where(QueuedPlan.account_id.in_(ids))  # type: ignore[arg-type]
        .where(QueuedPlan.status == QueuedPlanStatus.activated)
        .order_by(QueuedPlan.activated_at, QueuedPlan.id)
    ).all()
    events = session.exec(
        select(AccountEvent)
        .where(AccountEvent.account_id.in_(ids))  # type: ignore[arg-type]
        .where(AccountEvent.action.in_(MARKER_ACTIONS))  # type: ignore[arg-type]
        .where(AccountEvent.date >= since_naive)
        .where(AccountEvent.date <= until_naive)
        .order_by(AccountEvent.date, AccountEvent.id)
    ).all()

    # Names in two extra queries (one per table), not one per account.
    customer_ids = {a.customer_id for a in accounts if a.customer_id is not None}
    group_ids = {a.group_id for a in accounts if a.group_id is not None}
    customer_names = {
        c.id: c.name
        for c in session.exec(select(Customer).where(Customer.id.in_(customer_ids))).all()  # type: ignore[arg-type]
    } if customer_ids else {}
    group_names = {
        g.id: g.name
        for g in session.exec(select(Group).where(Group.id.in_(group_ids))).all()  # type: ignore[arg-type]
    } if group_ids else {}

    def utc(value: Optional[datetime]) -> Optional[datetime]:
        # Stored naive-UTC; attach the marker so the serialized ISO string
        # states that truth instead of leaving clients to guess.
        return value.replace(tzinfo=timezone.utc) if value is not None else None

    rows_by_account: dict[int, list[LedgerEntry]] = {i: [] for i in ids}
    for row in rows:
        rows_by_account[row.account_id].append(row)

    return ChargeHistoryResponse(
        accounts=[
            HistoryAccount(
                id=a.id,
                username=a.marzban_username,
                status=a.status,
                deleted=a.deleted_at is not None,
                customer_id=a.customer_id,
                customer_name=customer_names.get(a.customer_id) if a.customer_id is not None else None,
                group_id=a.group_id,
                group_name=group_names.get(a.group_id) if a.group_id is not None else None,
                billing_mode=a.billing_mode,
            )
            for a in ordered_accounts
        ],
        entries=[
            HistoryEntry(
                id=r.id,
                account_id=r.account_id,
                date=utc(r.date),
                type=r.type,
                amount=r.amount,
                gb_amount=r.gb_amount,
                consumed_gb=r.consumed_gb,
                source=r.source,
                created_by=r.created_by,
                note=r.note,
            )
            # include_credits only hides credits from the ENTRY list — the
            # summaries below are computed from the unfiltered rows.
            for r in rows
            if include_credits or r.type == LedgerType.charge
        ],
        packages=[
            HistoryPackage(
                account_id=p.account_id,
                activated_at=utc(p.activated_at),
                data_limit_gb=p.data_limit_gb,
                duration_days=p.duration_days,
            )
            for p in packages
            if p.activated_at is not None
        ],
        markers=[
            HistoryMarker(
                account_id=e.account_id,
                date=utc(e.date),
                action=e.action,
                detail=e.detail,
            )
            for e in events
        ],
        summaries={str(i): _summarize(rows_by_account[i]) for i in ids},
        totals=_summarize(list(rows)),
    )
