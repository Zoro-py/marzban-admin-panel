"""Account / customer / group STATEMENT for a date window — the data behind
the "Balance since" widget, shaped like a bank statement so it can't be
misread:

    opening balance (posted before `since`)
  + charges in the window
  - payments in the window
  = closing posted balance
  + not invoiced yet (live, window-blind)
  = owed now                       (== the page's «Owes now», by construction)

…plus the two things a money figure alone never answered: how much VOLUME was
added (recharges: packages activated, panel/delegate extensions, changes made
directly in Marzban) and how much was really CONSUMED — read from Marzban's
own per-node usage history for the exact window (resets don't erase it), not
inferred from invoices.

Read-only: no write, no commit, no Marzban mutation. Money figures come from
the same single-owner bucketing MoneyBook uses (an entry belongs to its
account if it has one, else its group, else its customer), so the scope's
rows here are exactly the rows MoneyBook sums — and the test suite pins
closing_posted == MoneyBook's all-time posted balance.

Recharge rows come from structured columns (QueuedPlan) or from AccountEvent
details that this codebase itself writes in a fixed machine format
(`extend_gb=<n>` in accounts.adjust / delegate_service, `+<n> GB added
outside this dashboard` in sync_job); a detail that doesn't match the exact
pattern is skipped, never guessed at. Changes made directly in Marzban are
reported in their own bucket and counted toward "recharged" only for accounts
billed prepay today — on payg accounts they are cap moves (e.g. the 300 GB
standard-shape sweep), not volume sold.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import jdatetime
from sqlalchemy import and_, or_
from sqlmodel import Session, select

from app.marzban_client import MarzbanAuthError, MarzbanUnavailable, marzban_client
from app.models import (
    Account, AccountEvent, BillingMode, Customer, Group, LedgerEntry, LedgerType,
    QueuedPlan, QueuedPlanStatus, utcnow,
)
from app.services import GB, MoneyBook, effective_billing_mode, effective_rate

log = logging.getLogger(__name__)

# Iran has had no DST since 2022: Tehran is a fixed +03:30.
TEHRAN = timezone(timedelta(hours=3, minutes=30))

MAX_ENTRIES = 500
# Live Marzban calls per statement: one per (account × month) for the monthly
# breakdown; beyond this we fall back to one call per account (window total
# only) so a big family over a long window can't fan out into hundreds of
# requests.
MAX_USAGE_CALLS = 60
MAX_MONTHS = 13
USAGE_CONCURRENCY = 6

# First moment Marzban's PER-USER usage history can be trusted as "what this
# account consumed". Measured 2026-10-10 on live data: before 2026-09-20 most
# traffic ran through the Dallas nodes, and deleting a node deletes its
# per-user usage rows — fleet per-user totals read 8% of node traffic on
# 09-18/19 and ramp 12% -> 54% over 09-20..09-24. From 09-25, 31 of 38 spot-
# checked accounts match the billing meter (used_traffic since their last
# reset) at 98-100% (the rest 60-97%, likely reset-time differences). Months
# before this are reported as "not recorded", never as 0.
USAGE_HISTORY_FROM = datetime(2026, 9, 25)

_EXTEND_GB = re.compile(r"^extend_gb=(-?\d+(?:\.\d+)?)(?:,|$| )")
_EXTERNAL_GB = re.compile(r"^\+(\d+(?:\.\d+)?) GB added outside this dashboard")
# Code-generated audit details (routers/accounts.py): billing_change carries
# the NEW mode; relationship_change is str() of the changed fields.
_BILLING_MODE = re.compile(r"billing_mode=(?:BillingMode\.)?(payg|prepay)")
_REL_GROUP = re.compile(r"'group_id': (None|\d+)")

# (username, start, end) -> (expires_at, bytes|None). Only windows that ended
# more than an hour ago are cached for long — Marzban buckets hourly, so a
# closed window's usage never changes; an open window is cached briefly so
# flipping tabs doesn't re-hit Marzban.
_usage_cache: dict[tuple[str, str, str], tuple[float, Optional[int]]] = {}


def _naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _jalali_month_starts(since: datetime, until: datetime) -> list[tuple[str, datetime, datetime]]:
    """Calendar (Shamsi) months intersecting [since, until), each clipped to
    the window: (label 'YYYY/MM', start_utc, end_utc). Month edges are Tehran
    midnight, converted to naive UTC to compare with stored rows."""
    out: list[tuple[str, datetime, datetime]] = []
    local = since.replace(tzinfo=timezone.utc).astimezone(TEHRAN)
    j = jdatetime.date.fromgregorian(date=local.date())
    jy, jm = j.year, j.month
    while True:
        first = jdatetime.date(jy, jm, 1).togregorian()
        ny, nm = (jy + 1, 1) if jm == 12 else (jy, jm + 1)
        nxt = jdatetime.date(ny, nm, 1).togregorian()
        m_start = datetime(first.year, first.month, first.day, tzinfo=TEHRAN).astimezone(timezone.utc).replace(tzinfo=None)
        m_end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=TEHRAN).astimezone(timezone.utc).replace(tzinfo=None)
        start, end = max(m_start, since), min(m_end, until)
        if start < end:
            out.append((f"{jy}/{jm:02d}", start, end))
        if m_end >= until:
            break
        jy, jm = ny, nm
    # The most RECENT months are the ones worth a row; older ones are dropped
    # from the table (the window totals above them still include everything).
    return out[-MAX_MONTHS:]


@dataclass
class Scope:
    kind: str
    id: int
    name: str
    accounts: list[Account]
    group_ids: list[int] = field(default_factory=list)
    customer_id: Optional[int] = None


def resolve_scope(session: Session, book: MoneyBook, *, account_id=None, group_id=None, customer_id=None) -> Optional[Scope]:
    if account_id is not None:
        a = session.get(Account, account_id)
        return Scope("account", a.id, a.marzban_username, [a]) if a else None
    if group_id is not None:
        g = session.get(Group, group_id)
        if not g:
            return None
        return Scope("group", g.id, g.name, list(book.group_members(g)), group_ids=[g.id])
    c = session.get(Customer, customer_id)
    if not c:
        return None
    groups = book.represented_groups(c)
    accounts = list(book.customer_accounts(c))
    for g in groups:
        accounts += list(book.group_members(g))
    return Scope("customer", c.id, c.name, accounts, group_ids=[g.id for g in groups], customer_id=c.id)


def _scope_condition(scope: Scope):
    """Exactly the ledger rows MoneyBook attributes to this scope."""
    ids = [a.id for a in scope.accounts]
    parts = []
    if ids:
        parts.append(LedgerEntry.account_id.in_(ids))
    if scope.group_ids:
        parts.append(and_(LedgerEntry.account_id.is_(None), LedgerEntry.group_id.in_(scope.group_ids)))
    if scope.customer_id is not None:
        parts.append(and_(LedgerEntry.account_id.is_(None), LedgerEntry.group_id.is_(None),
                          LedgerEntry.customer_id == scope.customer_id))
    return or_(*parts) if parts else LedgerEntry.id == -1


def _signed(e: LedgerEntry) -> float:
    return e.amount if e.type == LedgerType.charge else -e.amount


async def _usage(username: str, start: datetime, end: datetime, sem: asyncio.Semaphore) -> tuple[Optional[int], Optional[str]]:
    key = (username, _iso(start), _iso(end))
    now = time.monotonic()
    hit = _usage_cache.get(key)
    if hit and hit[0] > now:
        return hit[1], None
    async with sem:
        try:
            used = await marzban_client.get_user_usage_bytes(username, key[1], key[2])
        except (MarzbanUnavailable, MarzbanAuthError) as exc:
            return None, str(exc)
        except Exception as exc:  # one bad account must not sink the statement
            log.warning("usage read failed for %s: %s", username, exc)
            return None, f"{type(exc).__name__}"
    closed = end < utcnow().replace(tzinfo=None) - timedelta(hours=1)
    _usage_cache[key] = (now + (3600 if closed else 60), used)
    return used, None


class _ModeHistory:
    """An account's EFFECTIVE billing mode at a past moment, rebuilt from the
    panel's own audit trail — needed to tell a volume sale made directly in
    Marzban (prepay at the time) from a payg cap move (e.g. the 300 GB
    standard-shape sweep). Grouped then → the group's mode (group mode changes
    aren't journaled, so its CURRENT mode stands in); standalone → the mode
    set by the last billing_change at or before that moment; before any
    recorded change → the account's mode today if it never changed, else
    prepay (every account here started on packages; payg came later)."""

    def __init__(self, session: Session, accounts: list[Account]):
        self.session = session
        self.accounts = {a.id: a for a in accounts}
        self.billing: dict[int, list[tuple[datetime, str]]] = defaultdict(list)
        self.groups: dict[int, list[tuple[datetime, Optional[int]]]] = defaultdict(list)
        self._group_mode: dict[int, Optional[BillingMode]] = {}
        if not accounts:
            return
        for ev in session.exec(select(AccountEvent).where(
                AccountEvent.account_id.in_(list(self.accounts)),
                AccountEvent.action.in_(["billing_change", "relationship_change"])).order_by(AccountEvent.date)).all():
            if ev.action == "billing_change":
                m = _BILLING_MODE.search(ev.detail or "")
                if m:
                    self.billing[ev.account_id].append((ev.date, m.group(1)))
            else:
                m = _REL_GROUP.search(ev.detail or "")
                if m:
                    self.groups[ev.account_id].append((ev.date, None if m.group(1) == "None" else int(m.group(1))))

    def _group_mode_of(self, gid: int) -> Optional[BillingMode]:
        if gid not in self._group_mode:
            g = self.session.get(Group, gid)
            self._group_mode[gid] = g.billing_mode if g else None
        return self._group_mode[gid]

    def at(self, account_id: int, when: datetime) -> BillingMode:
        a = self.accounts[account_id]
        hist = self.groups.get(account_id, [])
        prior = [g for d, g in hist if d <= when]
        gid = prior[-1] if prior else (None if hist else a.group_id)
        if gid is not None:
            gm = self._group_mode_of(gid)
            if gm is not None:
                return BillingMode(gm)
        changes = self.billing.get(account_id, [])
        before = [mode for d, mode in changes if d <= when]
        if before:
            return BillingMode(before[-1])
        return BillingMode.prepay if changes else BillingMode(a.billing_mode)


async def build_statement(session: Session, *, since: datetime, until: Optional[datetime] = None,
                          account_id=None, group_id=None, customer_id=None, with_usage: bool = True) -> Optional[dict]:
    now = utcnow().replace(tzinfo=None)
    since = _naive_utc(since)
    until = _naive_utc(until) if until is not None else now
    if until > now:
        until = now

    book = MoneyBook(session)
    scope = resolve_scope(session, book, account_id=account_id, group_id=group_id, customer_id=customer_id)
    if scope is None:
        return None
    acc_by_id = {a.id: a for a in scope.accounts}
    cond = _scope_condition(scope)

    rows = session.exec(select(LedgerEntry).where(cond).order_by(LedgerEntry.date, LedgerEntry.id)).all()
    opening = round(sum(_signed(e) for e in rows if e.date < since), 2)
    window = [e for e in rows if since <= e.date < until]
    after = [e for e in rows if e.date >= until]
    charges = [e for e in window if e.type == LedgerType.charge]
    credits = [e for e in window if e.type == LedgerType.credit]
    charged = round(sum(e.amount for e in charges), 2)
    paid = round(sum(e.amount for e in credits), 2)
    closing = round(opening + charged - paid, 2)
    all_time_posted = round(closing + sum(_signed(e) for e in after), 2)

    if scope.kind == "account":
        pending = book.account_pending(scope.accounts[0])
    elif scope.kind == "group":
        pending = book.group_pending(session.get(Group, scope.id))
    else:
        pending = book.customer_pending(session.get(Customer, scope.id))
    pending = round(pending, 2)

    # --- per-account context -------------------------------------------------
    groups = {g: session.get(Group, g) for g in {a.group_id for a in scope.accounts if a.group_id}}
    modes = {a.id: effective_billing_mode(session, a, groups.get(a.group_id)) for a in scope.accounts}
    rates = {a.id: effective_rate(session, a, groups.get(a.group_id)) for a in scope.accounts}

    # --- recharges (volume added) ---------------------------------------------
    history = _ModeHistory(session, scope.accounts)
    recharges: list[dict] = []
    ids = list(acc_by_id)
    if ids:
        for p in session.exec(select(QueuedPlan).where(
                QueuedPlan.account_id.in_(ids), QueuedPlan.status == QueuedPlanStatus.activated,
                QueuedPlan.activated_at >= since, QueuedPlan.activated_at < until)).all():
            recharges.append({"date": p.activated_at, "account_id": p.account_id, "kind": "package",
                              "gb": float(p.data_limit_gb), "days": p.duration_days})
        for ev in session.exec(select(AccountEvent).where(
                AccountEvent.account_id.in_(ids),
                AccountEvent.action.in_(["adjust", "delegate_renew", "external_data_limit_increase"]),
                AccountEvent.date >= since, AccountEvent.date < until)).all():
            if ev.action == "external_data_limit_increase":
                m = _EXTERNAL_GB.match(ev.detail or "")
                if not m:
                    continue
                kind = "outside_panel"
            else:
                m = _EXTEND_GB.match(ev.detail or "")
                if not m:
                    continue
                kind = "delegate" if ev.action == "delegate_renew" else "panel"
            gb = float(m.group(1))
            if gb == 0:
                continue
            counts = kind != "outside_panel" or history.at(ev.account_id, ev.date) == BillingMode.prepay
            recharges.append({"date": ev.date, "account_id": ev.account_id, "kind": kind, "gb": gb,
                              "counted": counts if gb > 0 else False})
    for r in recharges:
        r.setdefault("counted", r["gb"] > 0)
        r["account"] = acc_by_id[r["account_id"]].marzban_username
    recharges.sort(key=lambda r: r["date"])
    counted = [r for r in recharges if r["counted"]]
    recharged_gb = round(sum(r["gb"] for r in counted), 3)

    # --- real usage from Marzban -------------------------------------------------
    # Only the part of the window inside Marzban's trustworthy per-user history
    # (see USAGE_HISTORY_FROM) is measured; earlier months report None
    # ("not recorded"), never a misleading 0.
    # Month rows start at the scope's first activity (an all-time window from
    # 2000 would otherwise list 26 empty years) and keep the most recent ones.
    first_activity = min([e.date for e in rows] + [r["date"] for r in recharges]
                         + [a.created_at for a in scope.accounts if a.created_at] or [until])
    months = _jalali_month_starts(max(since, first_activity), until) if since < until else []
    usage_since = max(since, USAGE_HISTORY_FROM)
    usage_months = [(lbl, max(s, USAGE_HISTORY_FROM), e) for lbl, s, e in months if e > USAGE_HISTORY_FROM]
    live_accounts = [a for a in scope.accounts if a.status != "deleted_from_marzban" and a.deleted_at is None]
    usage_by_acc: dict[int, Optional[float]] = {a.id: None for a in scope.accounts}
    usage_by_month: dict[str, Optional[float]] = {m[0]: None for m in months}
    usage_errors: list[str] = []
    usage_status = "skipped"
    if usage_since >= until:
        usage_status = "before_history"
    elif with_usage and live_accounts:
        sem = asyncio.Semaphore(USAGE_CONCURRENCY)
        # Per-month reads only when the month rows cover the whole measured
        # span (they keep only the latest MAX_MONTHS); otherwise one read per
        # account for the window total.
        covered = bool(usage_months) and usage_months[0][1] <= usage_since
        per_month = covered and len(usage_months) * len(live_accounts) <= MAX_USAGE_CALLS
        if per_month:
            jobs = [(a, m) for a in live_accounts for m in usage_months]
            res = await asyncio.gather(*(_usage(a.marzban_username, m[1], m[2], sem) for a, m in jobs))
            acc_tot: dict[int, float] = defaultdict(float)
            mon_tot: dict[str, float] = defaultdict(float)
            failed_acc: set[int] = set()
            for (a, m), (used, err) in zip(jobs, res):
                if err:
                    failed_acc.add(a.id)
                    usage_errors.append(f"{a.marzban_username}: {err}")
                    continue
                gb = (used or 0) / GB
                acc_tot[a.id] += gb
                mon_tot[m[0]] += gb
            for a in live_accounts:
                usage_by_acc[a.id] = None if a.id in failed_acc else round(acc_tot[a.id], 3)
            if not failed_acc:
                usage_by_month.update({m[0]: round(mon_tot[m[0]], 3) for m in usage_months})
        else:
            res = await asyncio.gather(*(_usage(a.marzban_username, usage_since, until, sem) for a in live_accounts))
            for a, (used, err) in zip(live_accounts, res):
                if err:
                    usage_errors.append(f"{a.marzban_username}: {err}")
                else:
                    usage_by_acc[a.id] = round((used or 0) / GB, 3)
        if usage_errors:
            usage_status = "partial" if any(v is not None for v in usage_by_acc.values()) else "unavailable"
        else:
            usage_status = "complete" if usage_since == since else "from_history_start"
    known_usage = [v for v in usage_by_acc.values() if v is not None]
    used_gb = round(sum(known_usage), 3) if known_usage else None

    # --- monthly breakdown --------------------------------------------------------
    month_rows = []
    for label, m_start, m_end in months:
        mc = [e for e in charges if m_start <= e.date < m_end]
        mp = [e for e in credits if m_start <= e.date < m_end]
        mr = [r for r in counted if m_start <= r["date"] < m_end]
        month_rows.append({
            "month": label, "start": m_start, "end": m_end,
            "charged": round(sum(e.amount for e in mc), 2), "charge_count": len(mc),
            "paid": round(sum(e.amount for e in mp), 2), "payment_count": len(mp),
            "recharged_gb": round(sum(r["gb"] for r in mr), 3), "recharge_count": len(mr),
            "used_gb": usage_by_month.get(label),
            "usage_partial_month": m_start < USAGE_HISTORY_FROM < m_end,
        })

    # --- per-account breakdown ----------------------------------------------------
    acc_rows = []
    for a in scope.accounts:
        ac = [e for e in charges if e.account_id == a.id]
        ap = [e for e in credits if e.account_id == a.id]
        ar = [r for r in counted if r["account_id"] == a.id]
        if not (ac or ap or ar or usage_by_acc.get(a.id)) and (a.deleted_at is not None or a.status == "deleted_from_marzban"):
            continue
        acc_rows.append({
            "account_id": a.id, "username": a.marzban_username,
            "billing_mode": getattr(modes[a.id], "value", modes[a.id]), "rate": rates[a.id],
            "deleted": a.deleted_at is not None or a.status == "deleted_from_marzban",
            "charged": round(sum(e.amount for e in ac), 2), "charge_count": len(ac),
            "paid": round(sum(e.amount for e in ap), 2),
            "recharged_gb": round(sum(r["gb"] for r in ar), 3), "recharge_count": len(ar),
            "used_gb": usage_by_acc.get(a.id),
            "pending": round(book.account_pending(a), 2),
        })

    # --- transactions with running balance -----------------------------------------
    running = opening
    entries = []
    for e in window[:MAX_ENTRIES]:
        running = round(running + _signed(e), 2)
        entries.append({
            "id": e.id, "date": e.date, "type": getattr(e.type, "value", e.type), "amount": e.amount,
            "gb_amount": e.gb_amount, "consumed_gb": e.consumed_gb, "note": e.note,
            "account": acc_by_id[e.account_id].marzban_username if e.account_id in acc_by_id else None,
            "source": getattr(e.source, "value", e.source), "created_by": e.created_by,
            "balance_after": running,
        })

    last_payment = max((e.date for e in rows if e.type == LedgerType.credit), default=None)
    gb_known = [e for e in charges if e.gb_amount is not None]
    mode_values = {getattr(m, "value", m) for m in modes.values()}

    return {
        "scope": {"type": scope.kind, "id": scope.id, "name": scope.name,
                  "account_count": len([a for a in scope.accounts if a.deleted_at is None]),
                  "billing_modes": sorted(mode_values)},
        "window": {"since": since, "until": until, "is_open": until >= now - timedelta(minutes=1)},
        "statement": {
            "opening_balance": opening,
            "charged": charged, "charge_count": len(charges),
            "paid": paid, "payment_count": len(credits),
            "closing_balance": closing,
            "pending": pending,
            "owed_now": round(all_time_posted + pending, 2),
            "all_time_posted": all_time_posted,
        },
        "charges_gb": {"gb": round(sum(e.gb_amount for e in gb_known), 3) if gb_known else None,
                       "with_gb": len(gb_known), "of": len(charges)},
        "volume": {
            "recharged_gb": recharged_gb, "recharge_count": len(counted),
            "by_kind": {k: {"gb": round(sum(r["gb"] for r in counted if r["kind"] == k), 3),
                            "count": len([r for r in counted if r["kind"] == k])}
                        for k in ("package", "panel", "delegate", "outside_panel")},
            "excluded": [r for r in recharges if not r["counted"]],
            "used_gb": used_gb,
            "usage_from": usage_since if usage_status not in ("before_history", "skipped") else None,
            "usage_history_from": USAGE_HISTORY_FROM,
            "usage_status": usage_status,
            "usage_errors": usage_errors[:10],
            # Ratios only when the measured usage covers the whole window — a
            # ratio of a full-window money figure over a partial usage figure
            # would be confidently wrong.
            "utilization_pct": round(used_gb / recharged_gb * 100, 1) if usage_status == "complete" and used_gb is not None and recharged_gb > 0 else None,
            "toman_per_used_gb": round(charged / used_gb) if usage_status == "complete" and used_gb and used_gb > 0.01 and charged > 0 else None,
        },
        "months": month_rows,
        "accounts": acc_rows,
        "recharges": counted,
        "entries": entries,
        "entries_truncated": len(window) > MAX_ENTRIES,
        "last_payment_at": last_payment,
    }
