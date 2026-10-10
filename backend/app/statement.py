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
from app.marzban_usage_log import load_periods
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

# Consumption source (D31): Marzban's reset log (app/marzban_usage_log.py)
# splits each user's life into periods between usage resets, each with its
# EXACT traffic. A span that fully covers a period gets it exactly; a span that
# cuts into the CURRENT period reads Marzban's per-node history, which is
# exact inside the current period (it is wiped at every reset, so it is never
# valid across one); a span that cuts into an OLD period gets that period
# prorated by time and is reported as an estimate. Never a silent 0.
# Two events are "the same moment" within this tolerance when matching a
# package activation / settle charge to a Marzban reset.
PERIOD_MATCH = timedelta(minutes=15)

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


def _prorate(used: int, lo: datetime, hi: datetime, p_start: datetime, p_end: datetime) -> float:
    """Time-proportional share of a period's exact total — an ESTIMATE, only
    used where a span cuts an old period (no finer data survives a reset)."""
    return used * (hi - lo).total_seconds() / max(1.0, (p_end - p_start).total_seconds())


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
    plans_by_acc: dict[int, list[QueuedPlan]] = defaultdict(list)
    if acc_by_id:
        for q in session.exec(select(QueuedPlan).where(
                QueuedPlan.account_id.in_(list(acc_by_id)), QueuedPlan.status == QueuedPlanStatus.activated,
                QueuedPlan.activated_at.is_not(None))).all():
            plans_by_acc[q.account_id].append(q)
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

    # --- real usage (Marzban reset log + current-period history) -----------------
    # Month rows start at the scope's first activity (an all-time window from
    # 2000 would otherwise list 26 empty years) and keep the most recent ones.
    live_accounts = [a for a in scope.accounts if a.status != "deleted_from_marzban" and a.deleted_at is None]
    periods_by_user, why = (load_periods([a.marzban_username for a in live_accounts])
                            if with_usage and live_accounts else ({}, None))
    # Marzban's own period starts count as activity too: a user created in
    # Marzban before its first ledger row still consumed in that time.
    period_starts = [ps.start for pl in (periods_by_user or {}).values() for ps in pl if ps.start > datetime(2001, 1, 1)]
    first_activity = min([e.date for e in rows] + [r["date"] for r in recharges] + period_starts
                         + [a.created_at for a in scope.accounts if a.created_at] or [until])
    months = _jalali_month_starts(max(since, first_activity), until) if since < until else []
    usage_by_acc: dict[int, Optional[float]] = {a.id: None for a in scope.accounts}
    est_by_acc: dict[int, float] = defaultdict(float)
    usage_by_month: dict[str, Optional[float]] = {m[0]: None for m in months}
    est_by_month: dict[str, float] = defaultdict(float)
    usage_errors: list[str] = []
    usage_status = "skipped"
    period_rows: list[dict] = []
    if with_usage and live_accounts:
        if periods_by_user is None:
            usage_status = "unavailable"
            usage_errors.append(why or "unavailable")
        else:
            spans = [("__window__", since, until)] + [(m[0], m[1], m[2]) for m in months]
            exact: dict[tuple[int, str], float] = defaultdict(float)
            est: dict[tuple[int, str], float] = defaultdict(float)
            history_jobs: list[tuple] = []
            ok_accounts: list[Account] = []
            for a in live_accounts:
                plist = periods_by_user.get(a.marzban_username)
                if plist is None:
                    usage_errors.append(f"{a.marzban_username}: unknown to Marzban")
                    continue
                ok_accounts.append(a)
                for key, s_, u_ in spans:
                    for p in plist:
                        p_end = p.end or now
                        lo, hi = max(s_, p.start), min(u_, p_end)
                        if lo >= hi:
                            continue
                        if s_ <= p.start and u_ >= p_end:
                            exact[(a.id, key)] += p.used_bytes
                        elif p.end is None:
                            history_jobs.append((a, key, lo, hi, p.used_bytes, p.start, p_end))
                        else:
                            est[(a.id, key)] += _prorate(p.used_bytes, lo, hi, p.start, p_end)
                # package-vs-usage table: every period overlapping the window
                acc_plans = [q for q in plans_by_acc.get(a.id, [])]
                for p in plist:
                    p_end = p.end or now
                    if p_end <= since or p.start >= until:
                        continue
                    plan = next((q for q in acc_plans if abs((q.activated_at - p.start).total_seconds()) <= PERIOD_MATCH.total_seconds()), None)
                    package_gb = float(plan.data_limit_gb) if plan else None
                    if package_gb is None and p.end is None and a.data_limit and history.at(a.id, now) == BillingMode.prepay:
                        package_gb = round(a.data_limit / GB, 3)
                    # The period's bill: the charge posted when it ended (auto-settle /
                    # settle reset happen in the same second), or any charge whose
                    # note names this period's package ("QueuedPlan #<id>" — how
                    # manual recovery charges reference the package they bill).
                    plan_ref = re.compile(rf"QueuedPlan #{plan.id}\b") if plan else None
                    settle = [e for e in rows if e.account_id == a.id and e.type == LedgerType.charge and (
                        (p.end is not None and abs((e.date - p.end).total_seconds()) <= PERIOD_MATCH.total_seconds())
                        or (plan_ref is not None and plan_ref.search(e.note or "")))]
                    period_rows.append({
                        "account_id": a.id, "account": a.marzban_username,
                        "start": p.start, "end": p.end, "is_open": p.end is None,
                        "mode": history.at(a.id, p.start).value,
                        "used_gb": round(p.used_bytes / GB, 3),
                        "package_gb": package_gb,
                        "used_pct": round(p.used_bytes / GB / package_gb * 100, 1) if package_gb else None,
                        "billed": round(sum(e.amount for e in settle), 2) if settle else None,
                        "billed_gb": next((e.gb_amount for e in settle if e.gb_amount is not None), None),
                        "pending": round(book.account_pending(a), 2) if p.end is None else None,
                        "partial_in_window": p.start < since or p_end > until,
                    })
            # Sub-spans of the CURRENT period: Marzban's node history is exact
            # there (it was wiped at the period's start, never before it).
            if history_jobs:
                sem = asyncio.Semaphore(USAGE_CONCURRENCY)
                jobs = history_jobs[:MAX_USAGE_CALLS]
                res = await asyncio.gather(*(_usage(j[0].marzban_username, j[2], j[3], sem) for j in jobs))
                for (a, key, lo, hi, p_used, p_start, p_end), (used, err) in zip(jobs, res):
                    if err or used is None:
                        est[(a.id, key)] += _prorate(p_used, lo, hi, p_start, p_end)
                    else:
                        exact[(a.id, key)] += min(used, p_used)
                for a, key, lo, hi, p_used, p_start, p_end in history_jobs[MAX_USAGE_CALLS:]:
                    est[(a.id, key)] += _prorate(p_used, lo, hi, p_start, p_end)
            for a in ok_accounts:
                usage_by_acc[a.id] = round((exact[(a.id, "__window__")] + est[(a.id, "__window__")]) / GB, 3)
                est_by_acc[a.id] = round(est[(a.id, "__window__")] / GB, 3)
            for label, _, _ in months:
                if ok_accounts:
                    usage_by_month[label] = round(sum(exact[(a.id, label)] + est[(a.id, label)] for a in ok_accounts) / GB, 3)
                    est_by_month[label] = round(sum(est[(a.id, label)] for a in ok_accounts) / GB, 3)
            any_est = any(v > 0.0005 for v in est_by_acc.values())
            if usage_errors:
                usage_status = "partial" if ok_accounts else "unavailable"
            else:
                usage_status = "estimated" if any_est else "complete"
    known_usage = [v for v in usage_by_acc.values() if v is not None]
    used_gb = round(sum(known_usage), 3) if known_usage else None
    used_estimated_gb = round(sum(est_by_acc.values()), 3)
    period_rows.sort(key=lambda r: (r["start"], r["account"]))

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
            "used_estimated_gb": est_by_month.get(label, 0.0),
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
            "used_estimated_gb": est_by_acc.get(a.id, 0.0),
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
            "used_estimated_gb": used_estimated_gb,
            "usage_status": usage_status,
            "usage_errors": usage_errors[:10],
            # Ratios only when the measured usage covers the whole window — a
            # ratio of a full-window money figure over a partial usage figure
            # would be confidently wrong.
            "utilization_pct": round(used_gb / recharged_gb * 100, 1) if usage_status == "complete" and used_gb is not None and recharged_gb > 0 else None,
            "toman_per_used_gb": round(charged / used_gb) if usage_status == "complete" and used_gb and used_gb > 0.01 and charged > 0 else None,
        },
        "months": month_rows,
        "periods": period_rows,
        "accounts": acc_rows,
        "recharges": counted,
        "entries": entries,
        "entries_truncated": len(window) > MAX_ENTRIES,
        "last_payment_at": last_payment,
    }
