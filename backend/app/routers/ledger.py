import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlmodel import Session, select

from app.auth import require_auth
from app.db import get_session
from app.models import Account, Customer, Group, LedgerEntry
from app.schemas import BalanceRead, LedgerCreate, LedgerRead
from app.services import MoneyBook

router = APIRouter(prefix="/api/ledger", tags=["ledger"], dependencies=[Depends(require_auth)])

# Optional client-supplied idempotency (header `Idempotency-Key`): a double-click,
# a second tab or an automatic retry that carries the SAME key returns the entry
# the first request wrote instead of appending a second one. The ledger is
# append-only, so a duplicated row can't be noticed afterwards — it silently
# invents (credit) or forgives (charge) money. Requests without the header behave
# exactly as before, so a legitimately repeated identical entry is never blocked.
# In-memory on purpose: the backend is one process (see services.serialise_billing),
# a restart only forgets keys that are minutes old, and no schema change is needed.
_IDEM_TTL_ENTRIES = 2000
_idem_lock = threading.Lock()
_idem_seen: dict[str, tuple[int, str]] = {}   # key -> (entry id, request fingerprint)


@router.get("", response_model=list[LedgerRead])
def list_ledger(
    customer_id: Optional[int] = None,
    group_id: Optional[int] = None,
    account_id: Optional[int] = None,
    offset: int = 0,
    limit: int = 200,
    session: Session = Depends(get_session),
):
    stmt = select(LedgerEntry).order_by(LedgerEntry.date.desc()).offset(offset).limit(limit)
    if customer_id is not None:
        stmt = stmt.where(LedgerEntry.customer_id == customer_id)
    if group_id is not None:
        stmt = stmt.where(LedgerEntry.group_id == group_id)
    if account_id is not None:
        stmt = stmt.where(LedgerEntry.account_id == account_id)
    return session.exec(stmt).all()


@router.post("", response_model=LedgerRead)
def create_ledger_entry(
    body: LedgerCreate,
    session: Session = Depends(get_session),
    operator: str = Depends(require_auth),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key", max_length=80),
):
    if idempotency_key:
        # Held across check + write + commit so two concurrent requests with one
        # key can't both pass the check (the endpoint runs in a thread pool).
        with _idem_lock:
            fingerprint = body.model_dump_json()
            seen = _idem_seen.get(idempotency_key)
            if seen is not None:
                seen_id, seen_fp = seen
                if seen_fp != fingerprint:
                    # Same key, different request: returning the first entry with a
                    # 200 would tell the caller their (different) payment was recorded.
                    raise HTTPException(409, "This Idempotency-Key was already used for a different entry")
                existing = session.get(LedgerEntry, seen_id)
                if existing is not None:
                    return existing
            entry = _write_entry(body, session, operator)
            _idem_seen[idempotency_key] = (entry.id, fingerprint)
            while len(_idem_seen) > _IDEM_TTL_ENTRIES:
                _idem_seen.pop(next(iter(_idem_seen)))
            return entry
    return _write_entry(body, session, operator)


def _write_entry(body: LedgerCreate, session: Session, operator: str) -> LedgerEntry:
    if body.customer_id is None and body.group_id is None:
        raise HTTPException(400, "Provide customer_id and/or group_id for this ledger entry")
    if body.customer_id is not None and not session.get(Customer, body.customer_id):
        raise HTTPException(404, "customer_id not found")
    if body.group_id is not None and not session.get(Group, body.group_id):
        raise HTTPException(404, "group_id not found")
    if body.account_id is not None and not session.get(Account, body.account_id):
        raise HTTPException(404, "account_id not found")
    if body.amount <= 0:
        raise HTTPException(400, "amount must be positive; use `type` to indicate charge vs credit")

    entry = LedgerEntry(**body.model_dump(), created_by=operator)
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return entry


@router.get("/balance", response_model=BalanceRead)
def get_balance(
    customer_id: Optional[int] = None,
    group_id: Optional[int] = None,
    account_id: Optional[int] = None,
    # "What do they owe FROM this date forward" — e.g. the date of their
    # last payment, so a running balance doesn't quietly drift out of sight
    # between manual reconciliations. Date-only (no time) is deliberately
    # accepted as-is: FastAPI parses "2026-09-01" into midnight that day,
    # which is the natural reading of "since the 1st" — entries posted
    # earlier that same day are correctly excluded.
    since: Optional[datetime] = None,
    session: Session = Depends(get_session),
):
    provided = [x is not None for x in (customer_id, group_id, account_id)]
    if sum(provided) != 1:
        raise HTTPException(400, "Provide exactly one of customer_id, group_id or account_id")

    # A timezone-AWARE `since` is normalised to naive UTC before it reaches
    # MoneyBook: SQLite's driver binds a datetime's own wall-clock fields
    # verbatim (no timezone conversion), so a non-UTC offset would otherwise
    # silently compare as that offset's wall time against the stored UTC
    # rows. The dashboard always sends UTC ("…Z"), for which this is an
    # identity — this pins the semantics for any future caller instead of
    # leaving them to an accident of the driver.
    if since is not None and since.tzinfo is not None:
        since = since.astimezone(timezone.utc).replace(tzinfo=None)

    # Roll-ups, not a raw scan of rows carrying this id — see
    # services.MoneyBook for why those two are not the same thing.
    book = MoneyBook(session, since=since)
    if customer_id is not None:
        customer = session.get(Customer, customer_id)
        if not customer:
            raise HTTPException(404, "customer_id not found")
        balance = book.customer_posted(customer)
        gb_charged, gb_consumed, charged_amount, consumed_amount = book.customer_gb(customer)
        credited_amount = book.customer_credits(customer)
        gb_pending = book.customer_gb_pending(customer)
        pending_amount = book.customer_pending(customer)
        pending_gb = book.customer_pending_gb(customer)
        meta = book.customer_charge_meta(customer)
        entity_type, entity_id = "customer", customer_id
    elif group_id is not None:
        group = session.get(Group, group_id)
        if not group:
            raise HTTPException(404, "group_id not found")
        balance = book.group_posted(group)
        gb_charged, gb_consumed, charged_amount, consumed_amount = book.group_gb(group)
        credited_amount = book.group_credits(group)
        gb_pending = book.group_gb_pending(group)
        pending_amount = book.group_pending(group)
        pending_gb = book.group_pending_gb(group)
        meta = book.group_charge_meta(group)
        entity_type, entity_id = "group", group_id
    else:
        account = session.get(Account, account_id)
        if not account:
            raise HTTPException(404, "account_id not found")
        balance = book.account_posted(account)
        gb_charged, gb_consumed, charged_amount, consumed_amount = book.account_gb(account)
        credited_amount = book.account_credits(account)
        gb_pending = book.account_gb_pending(account)
        pending_amount = book.account_pending(account)
        pending_gb = book.account_pending_gb(account)
        meta = book.account_charge_meta(account)
        entity_type, entity_id = "account", account_id

    # total_charge/total_credit are reported as the netted balance split into
    # its sign, rather than gross sums: a roll-up has no single meaningful
    # gross figure once it spans several accounts and groups.
    return BalanceRead(
        entity_type=entity_type,
        entity_id=entity_id,
        total_charge=max(0.0, balance),
        total_credit=max(0.0, -balance),
        balance=balance,
        # NULL (rendered "—") means no known-GB charge row in this window —
        # never re-interpreted as zero. gb_pending is live/open-cycle usage
        # and, like the money pending figure, deliberately window-blind.
        gb_charged=round(gb_charged, 3) if gb_charged is not None else None,
        gb_consumed=round(gb_consumed, 3) if gb_consumed is not None else None,
        gb_pending=gb_pending,
        charged_amount=round(charged_amount, 2) if charged_amount is not None else None,
        consumed_amount=round(consumed_amount, 2) if consumed_amount is not None else None,
        credited_amount=round(credited_amount, 2) if credited_amount is not None else None,
        pending_amount=round(pending_amount, 2),
        # Headline: what they owe from `since`, INCLUDING what isn't invoiced
        # yet. `pending_amount` is window-blind by nature (usage can't be
        # attributed to a sub-range), so this is exactly «posted since the
        # date» + «the open, not-yet-invoiced part» — the same two parts
        # «Owes now» adds, restricted on the posted side to the window.
        net_owed=round(balance + pending_amount, 2),
        pending_gb=round(pending_gb, 3),
        charge_count=meta[0],
        charge_count_with_gb=meta[1],
        charged_amount_gb_known=round(meta[2], 2),
    )
