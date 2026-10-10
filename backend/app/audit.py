"""Cross-account billing anomalies — the gap the "Statement" view can't close
on its own: Statement answers "is THIS account's billing right" once the
operator already thinks to open it. This answers "which of the ~170 accounts
might need a look", unprompted, in one read-only pass.

Built 2026-10-11 after auto_renew_enabled was turned on for nearly every
account and the operator found a live mis-billing (boojar_family: an
undocumented 4,000 T/GB verbal rate billed at the 5,000 default; see D33) only
by cross-referencing a Telegram chat by hand. A 6-model council (see
scripts/consult/vpn_autorenew_council.py in the sibling Shiraze repo, run
2026-10-11) was unanimous on two points this module encodes directly:

  1. None of this would have caught THAT specific incident up front — an
     arithmetic check can only compare the system's own numbers against each
     other; it cannot know about an agreement that was never entered
     anywhere. What it buys instead: once a rate IS corrected (as boojar's
     was), every older charge billed under the stale rate is surfaced for
     review automatically — the self-healing half of the same problem.
  2. The single highest-value, lowest-maintenance first build is a
     cross-account view, not a deeper per-account one (Statement already is
     one) and not a behavior change to auto-renew itself (the dampening/audit
     trail it already writes is sound; the gap is nobody reads 150+ accounts'
     worth of it).

IMPORTANT, found while first testing this against the live DB (not just
guessed): the first draft of two of these four checks produced false
positives that would have been wrong to show the operator, so they were
rewritten before this ever shipped —
  - rate_mismatch originally compared every historical charge to the
    account's CURRENT rate. That flags any account whose rate was ever
    legitimately changed later (e.g. switched to a comp/free 0 rate) — the
    charge was correct when it was posted. Rewritten to reconstruct the rate
    that was actually in effect AT CHARGE TIME from the RateChange audit
    table, so only a charge that was already wrong when posted is flagged.
  - the original "no_matching_bill" assumed every prepay settle coincides
    with a Marzban usage reset. It doesn't: settle_account's manual-settle
    path (routers/accounts.py) intentionally never resets Marzban for
    prepay (only payg does) — a manual/mid-cycle settlement is a normal,
    correct billing event that leaves the Marzban period running
    uninterrupted. Matching "charge near this period's end" against that
    flagged 84/154 accounts, nearly all real settlements wrongly called
    "unbilled". Replaced with stale_open_period: an account's CURRENT Marzban
    period has been open far longer than a package should ever run AND it
    still carries pending (unbilled) debt — verified against the live DB
    (2 genuine hits, both explained by the same real gap: no expiry date set,
    so near_expiry never fires and the account can run unsettled
    indefinitely regardless of elapsed time).

Four checks, all read-only, scoped to live PREPAY accounts only (a payg
account's data_limit is a technical soft cap, not a package — D30 already
established that a payg cap move is never a sale) and built ONLY from the
ledger/event data this panel already writes, plus Marzban's cheap local
reset-log read for period boundaries (no live Marzban HTTP calls, so this
scales to the whole fleet in one pass):

  - rate_mismatch: a charge that didn't match the rate actually in effect
    for this account AT THE TIME it was posted (reconstructed from
    RateChange). Catches a rate correction that wasn't back-applied to
    older charges (exactly today's incident, after the fix) and any future
    rate/charge drift — never a charge that was simply priced before a
    later, legitimate rate change.
  - external_change_unbilled: an external_data_limit_increase /
    external_usage_reset / external_expire_extend event (a change made
    directly in Marzban, bypassing the panel) with no charge or credit
    posted anywhere near it — the exact shape of boojar's missed-50GB
    question, generalized to every account instead of requiring the
    operator to already suspect one.
  - stale_open_period: a live prepay account's current (still-open) Marzban
    usage period has run longer than STALE_OPEN_PERIOD_DAYS with pending
    (unbilled) debt still sitting on it — near-quota/near-expiry auto-queue
    never fired for it (commonly: no expiry date set on the account, so the
    time-based half of that trigger can never fire at all).
  - dampened_resize: an auto-queued package whose size the sizing job itself
    capped below the raw trailing-average estimate (sync_job.py's
    _dampened_package_size_gb) — already flagged to the operator once, in a
    Telegram message among ~300/month; resurfaced here so it doesn't
    require remembering which chat to scroll to.

Nothing here writes anything. Every finding links back to the account so the
operator's next step is always "open Statement for this one account", not an
auto-fix — the fix is always their call (see D33: 2 of boojar's issues needed
a business decision, not code).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from sqlmodel import Session, select

from app.marzban_usage_log import load_periods
from app.models import Account, AccountEvent, BillingMode, Group, LedgerEntry, LedgerType, RateChange, utcnow
from app.services import MoneyBook, effective_billing_mode, effective_rate, get_default_rate

# A charge/credit posted up to this long after an external-change event
# still counts as "this covered it" — operators don't always bill the same
# minute they notice a direct Marzban change.
EXTERNAL_CHANGE_BILL_WINDOW = timedelta(hours=48)
# Below this absolute Toman gap, a rate_mismatch is float/rounding noise, not
# a real drift worth an operator's attention.
RATE_MISMATCH_MIN_TOMAN = 500.0
# Longer than any normal package duration (AUTO_NEXT_PLAN_DURATION_DAYS is
# 31) plus real slack for a manually-sized long package — past this, an
# account that still owes something is presumed stuck, not just mid-cycle.
STALE_OPEN_PERIOD_DAYS = 40
EXTERNAL_CHANGE_ACTIONS = ("external_data_limit_increase", "external_usage_reset", "external_expire_extend")


def _rate_at(rate_changes: list[RateChange], current_rate: float, default_rate: float, when) -> float:
    """The account's own effective rate as it stood at `when`, reconstructed
    from this account's RateChange rows (newest-not-after-`when` wins).

    Two different fallbacks, not one, because they answer different
    questions — collapsing them was the bug caught in live testing
    (Erfan_yazdi: rate explicitly zeroed out 2 days AFTER a charge that was
    correctly billed at the then-current default; falling back to today's
    CURRENT rate made that charge look wrong when it never was):
      - no RateChange rows at all for this account -> the rate has never
        changed -> `current_rate` (today's effective_rate) IS what it always
        was.
      - `when` predates every recorded change (this account's rate DID
        change, just not yet at `when`) -> a change's old/new_rate of None
        means "inherited" at that moment, not "zero" — the best read of
        what it was inherited AS is `default_rate` (today's dashboard-wide
        default; there are no group/default-scope RateChange rows in this
        panel's history to reconstruct a truer point-in-time value from)."""
    if not rate_changes:
        return current_rate
    applicable = [c for c in rate_changes if c.created_at <= when]
    if applicable:
        latest = max(applicable, key=lambda c: c.created_at)
        return latest.new_rate if latest.new_rate is not None else default_rate
    earliest = min(rate_changes, key=lambda c: c.created_at)
    return earliest.old_rate if earliest.old_rate is not None else default_rate


def build_anomalies(session: Session, *, lookback_days: int = 60) -> dict:
    """One pass over every live account. Returns {scanned, window, findings:
    [...]} — findings sorted newest-first, each tagged with its `kind` so the
    frontend can group/filter. Never raises on a single account's own data
    being odd (a missing Marzban period, say) — that account is just skipped
    for the checks that need it; findings from every other account and every
    other check still come back."""
    now = utcnow().replace(tzinfo=None)
    since = now - timedelta(days=lookback_days)

    # status is filtered in Python, not SQL: Account.status is Optional[str]
    # (defaults to None), and a SQL `!= "deleted_from_marzban"` silently
    # drops every NULL-status row (three-valued SQL logic) instead of
    # keeping them — caught by this module's own tests, not live data,
    # because live rows happen to always carry a status sync_job set.
    accounts = [a for a in session.exec(select(Account).where(Account.deleted_at.is_(None))).all()
                if a.status != "deleted_from_marzban"]
    groups = {g: session.get(Group, g) for g in {a.group_id for a in accounts if a.group_id}}
    by_id = {a.id: a for a in accounts}
    prepay_ids = {a.id for a in accounts if effective_billing_mode(session, a, groups.get(a.group_id)) == BillingMode.prepay}
    book = MoneyBook(session)
    default_rate = get_default_rate(session)

    charges = session.exec(
        select(LedgerEntry).where(
            LedgerEntry.account_id.in_(list(by_id)), LedgerEntry.type == LedgerType.charge, LedgerEntry.date >= since
        )
    ).all() if by_id else []
    charges_by_acc: dict[int, list[LedgerEntry]] = defaultdict(list)
    for e in charges:
        charges_by_acc[e.account_id].append(e)

    money_events = session.exec(
        select(LedgerEntry).where(LedgerEntry.account_id.in_(list(by_id)), LedgerEntry.date >= since - EXTERNAL_CHANGE_BILL_WINDOW)
    ).all() if by_id else []
    money_by_acc: dict[int, list[LedgerEntry]] = defaultdict(list)
    for e in money_events:
        money_by_acc[e.account_id].append(e)

    events = session.exec(
        select(AccountEvent).where(AccountEvent.account_id.in_(list(by_id)), AccountEvent.date >= since)
    ).all() if by_id else []

    rate_changes = session.exec(
        select(RateChange).where(RateChange.scope == "account", RateChange.account_id.in_(list(by_id)))
    ).all() if by_id else []
    rate_changes_by_acc: dict[int, list[RateChange]] = defaultdict(list)
    for c in rate_changes:
        rate_changes_by_acc[c.account_id].append(c)

    findings: list[dict] = []

    # --- 1) rate_mismatch: a charge that didn't match the rate in effect WHEN IT WAS POSTED ---
    for aid in prepay_ids:
        a = by_id[aid]
        current_rate = effective_rate(session, a, groups.get(a.group_id))
        history = rate_changes_by_acc.get(aid, [])
        for e in charges_by_acc.get(aid, []):
            if e.gb_amount is None or e.gb_amount <= 0:
                continue
            rate_then = _rate_at(history, current_rate, default_rate, e.date)
            expected = round(e.gb_amount * rate_then, 2)
            gap = round(e.amount - expected, 2)
            if abs(gap) >= RATE_MISMATCH_MIN_TOMAN:
                findings.append({
                    "kind": "rate_mismatch", "account_id": a.id, "account": a.marzban_username,
                    "date": e.date, "entry_id": e.id,
                    "detail": {
                        "charged": e.amount, "gb_amount": e.gb_amount, "rate_then": rate_then,
                        "current_rate": current_rate, "expected_at_rate_then": expected, "gap": gap,
                    },
                })

    # --- 2) external_change_unbilled: a direct-Marzban change with nothing posted near it ---
    for ev in events:
        if ev.action not in EXTERNAL_CHANGE_ACTIONS or ev.account_id not in prepay_ids:
            continue
        a = by_id[ev.account_id]
        lo, hi = ev.date - EXTERNAL_CHANGE_BILL_WINDOW, ev.date + EXTERNAL_CHANGE_BILL_WINDOW
        covered = any(lo <= m.date <= hi for m in money_by_acc.get(ev.account_id, []))
        if not covered:
            findings.append({
                "kind": "external_change_unbilled", "account_id": a.id, "account": a.marzban_username,
                "date": ev.date, "detail": {"action": ev.action, "note": ev.detail},
            })

    # --- 3) stale_open_period: current package open way too long, still owes something ---
    periods_by_user, usage_err = load_periods([a.marzban_username for a in accounts])
    if periods_by_user is not None:
        for aid in prepay_ids:
            a = by_id[aid]
            plist = periods_by_user.get(a.marzban_username)
            if not plist or plist[-1].end is not None:
                continue  # unknown to Marzban, or (shouldn't happen) no open period
            open_period = plist[-1]
            age_days = (now - open_period.start).total_seconds() / 86400
            pending = book.account_pending(a)
            if age_days >= STALE_OPEN_PERIOD_DAYS and pending > 0:
                findings.append({
                    "kind": "stale_open_period", "account_id": a.id, "account": a.marzban_username,
                    "date": open_period.start, "detail": {
                        "open_since": open_period.start, "age_days": round(age_days, 1),
                        "pending_amount": round(pending, 2), "auto_renew_enabled": a.auto_renew_enabled,
                        "has_expiry": bool(a.expire),
                    },
                })

    # --- 4) dampened_resize: the sizing job itself capped the raw estimate ---
    for ev in events:
        if ev.action == "next_plan_auto_queued" and "dampened from raw" in ev.detail and ev.account_id in prepay_ids:
            a = by_id[ev.account_id]
            findings.append({
                "kind": "dampened_resize", "account_id": a.id, "account": a.marzban_username,
                "date": ev.date, "detail": {"note": ev.detail},
            })

    findings.sort(key=lambda f: f["date"], reverse=True)
    return {
        "scanned_accounts": len(accounts),
        "window": {"since": since, "until": now, "days": lookback_days},
        "usage_log_available": periods_by_user is not None,
        "usage_log_error": usage_err,
        "counts": {k: sum(1 for f in findings if f["kind"] == k) for k in
                   ("rate_mismatch", "external_change_unbilled", "stale_open_period", "dampened_resize")},
        "findings": findings,
    }
