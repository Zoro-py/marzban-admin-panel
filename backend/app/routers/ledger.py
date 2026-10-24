import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlmodel import Session, select

from app.auth import require_auth
from app.db import get_session
from app.models import Account, Customer, Group, LedgerEntry
from app.schemas import (
    BalanceRead,
    LedgerCreate,
    LedgerRead,
    LedgerVoidRequest,
    LedgerVoidResult,
)
from app.services import (
    IdempotencyConflict,
    IdempotencyRegistry,
    MoneyBook,
    account_posted_balance,
    group_only_posted_balance,
    serialise_billing,
)

router = APIRouter(prefix="/api/ledger", tags=["ledger"], dependencies=[Depends(require_auth)])

# Optional client-supplied idempotency (header `Idempotency-Key`): a double-click,
# a second tab or an automatic retry that carries the SAME key returns the entry
# the first request wrote instead of appending a second one. The ledger is
# append-only, so a duplicated row can't be noticed afterwards — it silently
# invents (credit) or forgives (charge) money. Requests without the header behave
# exactly as before, so a legitimately repeated identical entry is never blocked.
# The mechanism lives in services.IdempotencyRegistry — shared with
# POST /api/accounts/{id}/adjust (F13) so the two endpoints cannot drift. This
# endpoint is sync, so its registry takes a threading.Lock (thread-pool
# executors genuinely race; see the class docstring for the async counterpart).
_idem = IdempotencyRegistry(lock=threading.Lock())


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
    def run():
        entry = _write_entry(body, session, operator)
        return entry, entry.id

    def fetch(stored_id):
        return session.get(LedgerEntry, stored_id)

    try:
        return _idem.run_sync(idempotency_key, body.model_dump_json(), run=run, fetch=fetch)
    except IdempotencyConflict:
        # Same key, different request: returning the first entry with a
        # 200 would tell the caller their (different) payment was recorded.
        raise HTTPException(409, "This Idempotency-Key was already used for a different entry")


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
    # A second, unfiltered book for the real all-time posted balance — only
    # built when `since` narrows the window at all (`since is None` means
    # `book` already IS the all-time view, so reuse it instead of a second
    # identical query). See BalanceRead.all_time_balance for why this exists.
    all_time_book = book if since is None else MoneyBook(session)
    if customer_id is not None:
        customer = session.get(Customer, customer_id)
        if not customer:
            raise HTTPException(404, "customer_id not found")
        balance = book.customer_posted(customer)
        all_time_balance = all_time_book.customer_posted(customer)
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
        all_time_balance = all_time_book.group_posted(group)
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
        all_time_balance = all_time_book.account_posted(account)
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
        all_time_balance=round(all_time_balance, 2),
        carried_over_balance=round(all_time_balance - balance, 2),
    )


@router.get("/statement")
async def get_statement(
    since: datetime,
    until: Optional[datetime] = None,
    customer_id: Optional[int] = None,
    group_id: Optional[int] = None,
    account_id: Optional[int] = None,
    usage: bool = True,
    session: Session = Depends(get_session),
):
    """Bank-statement view of one scope over [since, until): opening balance,
    charges, payments, closing balance, not-invoiced-yet, owed now — plus the
    volume side (GB recharged, GB really consumed per Marzban's own usage
    history), a Shamsi-month breakdown, a per-account breakdown and every
    transaction with its running balance. Read-only. See app/statement.py.

    `usage=false` skips the live Marzban reads (money-only, instant)."""
    provided = [x is not None for x in (customer_id, group_id, account_id)]
    if sum(provided) != 1:
        raise HTTPException(400, "Provide exactly one of customer_id, group_id or account_id")
    if until is not None and since >= until:
        raise HTTPException(400, "since must be before until")
    from app.statement import build_statement

    result = await build_statement(
        session, since=since, until=until,
        account_id=account_id, group_id=group_id, customer_id=customer_id, with_usage=usage,
    )
    if result is None:
        raise HTTPException(404, "Scope not found")
    return result


# ── void / unvoid (2026-10-24) ────────────────────────────────────────────────
#
# The owner-facing answer to "واریز اشتباه رو بتونم از حساب‌کتاب حذف کنم"
# WITHOUT breaking the append-only rule the ledger's correctness hangs on
# (models.LedgerEntry's class docstring, DOMAIN_AND_BILLING.md §1): the row is
# never deleted and its amount never edited — voiding STAMPS it
# (voided_at/by/reason), and every money computation in the system excludes
# stamped rows. The stamp itself is the audit trail: who withdrew it, when,
# and why (the reason is mandatory).
#
# Deliberately NOT compensated with a mirror entry: a compensating entry would
# keep both rows visible in every listing forever and double the noise a
# mistaken deposit already caused; the stamp keeps the audit (the row shows
# struck-through, with who/when/why) while every balance, statement, finance
# bucket, debt-nudge replay, audit pass and charge-history total drops it —
# verified exhaustively by tests/test_ledger_void.py.
#
# What voiding does NOT undo (documented, deliberate): side effects that
# already happened outside this table. A voided payg settle's charge does not
# re-open the meter epoch that was closed when it posted (the usage was really
# consumed and really reset — re-opening it would re-bill GB the meter says
# were billed), and a voided mark-paid credit does not un-check the
# MonthlySettlementBatch row (un-mark it by hand if that pairing matters).
# The void result's before/after balances are exactly where the operator sees
# the effect and decides.


def _owning_scope(session: Session, entry: LedgerEntry) -> tuple[str, int]:
    """The single owner a LedgerEntry is summed at (DOMAIN_AND_BILLING.md §1:
    account_id, else group_id, else customer_id — the other FK columns are
    display metadata, never a second bucket)."""
    if entry.account_id is not None:
        return "account", entry.account_id
    if entry.group_id is not None:
        return "group", entry.group_id
    if entry.customer_id is not None:
        return "customer", entry.customer_id
    raise HTTPException(400, "This entry has no owning scope (all of account/group/customer are NULL)")


def _scope_balance(session: Session, scope: str, scope_id: int) -> float:
    """The posted balance of exactly the bucket the entry lives in — the same
    figure a void moves. Group rows are measured against the GROUP-ONLY bucket
    (a member account's balance is a different bucket), customer rows against
    the customer roll-up MoneyBook attributes the customer-level bucket to."""
    if scope == "account":
        return account_posted_balance(session, scope_id)
    if scope == "group":
        return group_only_posted_balance(session, scope_id)
    customer = session.get(Customer, scope_id)
    if customer is None:
        raise HTTPException(404, "customer_id not found")
    return round(MoneyBook(session).customer_posted(customer), 2)


@router.post("/{entry_id}/void", response_model=LedgerVoidResult)
@serialise_billing
async def void_ledger_entry(
    entry_id: int,
    body: LedgerVoidRequest,
    session: Session = Depends(get_session),
    operator: str = Depends(require_auth),
):
    """Withdraw a mistaken entry. Takes the process-wide billing lock so a
    concurrent settle can't interleave its read-compute-write around the
    balance snapshot this returns. Idempotence is the STATE guard below, not
    an idempotency key: a second void of the same row is a 409, not a second
    stamp."""
    entry = session.get(LedgerEntry, entry_id)
    if not entry:
        raise HTTPException(404, "Ledger entry not found")
    if entry.voided_at is not None:
        raise HTTPException(409, "This entry is already voided")
    scope_name, scope_id = _owning_scope(session, entry)

    balance_before = _scope_balance(session, scope_name, scope_id)
    entry.voided_at = datetime.now(timezone.utc).replace(tzinfo=None)
    entry.voided_by = operator
    entry.void_reason = body.reason.strip()
    session.add(entry)
    session.commit()
    session.refresh(entry)
    balance_after = _scope_balance(session, scope_name, scope_id)
    return LedgerVoidResult(
        entry=LedgerRead.model_validate(entry, from_attributes=True),
        scope=scope_name,
        scope_id=scope_id,
        scope_balance_before=round(balance_before, 2),
        scope_balance_after=round(balance_after, 2),
    )


@router.post("/{entry_id}/unvoid", response_model=LedgerVoidResult)
@serialise_billing
async def unvoid_ledger_entry(
    entry_id: int,
    session: Session = Depends(get_session),
    operator: str = Depends(require_auth),
):
    """Undo a mistaken void — the safety valve for "I voided the wrong row".
    Clears the stamp; the row counts in every sum again. The fact that it was
    once voided is visible in the audit trail only as this row's past state
    (the void's reason is gone with the stamp — a fresh unvoid needs no
    reason, it is itself the operator's statement)."""
    entry = session.get(LedgerEntry, entry_id)
    if not entry:
        raise HTTPException(404, "Ledger entry not found")
    if entry.voided_at is None:
        raise HTTPException(409, "This entry is not voided")
    scope_name, scope_id = _owning_scope(session, entry)

    balance_before = _scope_balance(session, scope_name, scope_id)
    entry.voided_at = None
    entry.voided_by = None
    entry.void_reason = None
    session.add(entry)
    session.commit()
    session.refresh(entry)
    balance_after = _scope_balance(session, scope_name, scope_id)
    return LedgerVoidResult(
        entry=LedgerRead.model_validate(entry, from_attributes=True),
        scope=scope_name,
        scope_id=scope_id,
        scope_balance_before=round(balance_before, 2),
        scope_balance_after=round(balance_after, 2),
    )
