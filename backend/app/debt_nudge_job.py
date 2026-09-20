"""Heads-up for debt that's been sitting a while — the operator asked for
the bot to raise this itself instead of them having to remember to check
Finance, but explicitly NOT as noise: only customers whose debt has been
outstanding a real while, checked every other day, not per-charge or per-
sync. Each reminder message carries an inline button per debtor that opens
a payment-recording conversation in the bot (see bot/handlers/debt.py),
so acting on the reminder is one tap away.

Deliberately about DURATION, not amount — a customer who was just charged
5,000,000 Toman five minutes ago isn't "overdue" in any useful sense yet; a
customer who has owed 80,000 Toman for six weeks is the one actually worth a
nudge. Uses POSTED debt only (real ledger charges), never the pending/
unbilled estimate — "real debt" per the operator's own framing, not a moving
number that hasn't been invoiced yet.

Purely informational: no charge, no Marzban call, nothing to roll back or
retry. If a scheduled send fails, the next run (every other day) tries
again on its own — no self-healing "did this run already happen" tracking
needed, the kind the money-moving jobs (payg monthly settlement) require
to never silently skip a cycle."""

import logging
from datetime import datetime

from sqlmodel import Session, or_, select

from app.db import engine
from app.models import Account, Customer, Group, LedgerEntry, LedgerType, utcnow
from app.notify import notify_admin_with_buttons
from app.services import MoneyBook

log = logging.getLogger(__name__)

# How long a positive balance has to have persisted, continuously, before
# it's worth a proactive nudge — not configurable via .env on purpose (this
# is a judgment call about "when is debt actually stale," not a deployment
# concern like a sync interval); change the constant if the threshold is
# ever wrong in practice.
DEBT_NUDGE_MIN_DAYS = 14.0


def _customer_ledger_scope(session: Session, customer: Customer) -> list[LedgerEntry]:
    """Every LedgerEntry that counts toward this customer's own total —
    their own accounts, every group they represent (both that group's
    member accounts and its own unattributed entries), and entries posted
    directly against the customer — the exact same attribution MoneyBook
    sums, just returned as rows instead of a total so the age of the debt
    can be read off them too."""
    accounts = session.exec(select(Account).where(Account.customer_id == customer.id)).all()
    groups = session.exec(select(Group).where(Group.representative_customer_id == customer.id)).all()
    group_ids = [g.id for g in groups]
    if group_ids:
        accounts += session.exec(select(Account).where(Account.group_id.in_(group_ids))).all()
    account_ids = [a.id for a in accounts]

    conditions = [LedgerEntry.account_id.in_(account_ids)] if account_ids else []
    if group_ids:
        conditions.append((LedgerEntry.account_id.is_(None)) & (LedgerEntry.group_id.in_(group_ids)))
    conditions.append(
        (LedgerEntry.account_id.is_(None)) & (LedgerEntry.group_id.is_(None)) & (LedgerEntry.customer_id == customer.id)
    )
    return session.exec(select(LedgerEntry).where(or_(*conditions)).order_by(LedgerEntry.date.asc())).all()


def _debt_age_days(entries: list[LedgerEntry], now: datetime) -> float | None:
    """Walks this customer's own ledger rows in date order, tracking a
    running balance, and returns how long it's been since the balance last
    CROSSED from zero-or-below into positive and stayed there — "how long
    has the debt that exists right now actually existed," not a FIFO
    charge-by-charge aging (which would need to decide which specific old
    charge a later payment paid off; for a once-a-week nudge, "still in
    debt, and has been since X" is the useful fact, not which exact invoice
    a partial payment covered).

    Deliberately NOT "time since the balance last touched non-positive" —
    a customer who was fully paid off 34 days ago and charged again
    yesterday has 1-day-old debt, not 34-day-old debt, even though the
    balance was last exactly 0 a month ago. Every crossing back to positive
    resets the start point; a crossing to non-positive clears it until the
    next one.

    None if there is no ledger history at all, or the balance isn't
    currently positive (nothing to age right now)."""
    if not entries:
        return None
    running = 0.0
    debt_started_at = None
    for e in entries:
        prev = running
        running += e.amount if e.type == LedgerType.charge else -e.amount
        if prev <= 0 and running > 0:
            debt_started_at = e.date
        elif running <= 0:
            debt_started_at = None
    if running <= 0 or debt_started_at is None:
        return None
    # SQLite round-trips datetimes as naive; `now` here is expected naive too
    # (see caller) so this subtraction doesn't raise on an aware/naive mix.
    return (now - debt_started_at).total_seconds() / 86400


def _format_toman(amount: float) -> str:
    return f"{round(amount):,}"


# Telegram caps an inline keyboard at 100 buttons; two per row means 50
# debtors per message. Beyond that the OLDEST 50 get buttons and the message
# says how many more there are — the panel's Finance page stays the
# exhaustive view. (34 debtors today; this is a guard, not the norm.)
MAX_NUDGE_BUTTONS = 50

# Button labels cap at 64 chars — keep the name short enough that even a
# wide Toman figure fits on one phone row, two buttons side by side.
_MAX_BUTTON_NAME = 16


def _build_nudge(overdue: list[dict]) -> tuple[str, dict]:
    """The nudge message + its keyboard. The text is deliberately a short
    summary — the per-debtor detail lives IN the buttons, because the point
    of this message is to act, not to read: one button per debtor (two per
    row, oldest debt first) opens the bot's payment console for that
    customer (see bot/handlers/debt.py — bucket breakdown, «کامل» or a typed
    custom amount, and a final ✅ ثبت / ❌ لغو confirm before anything
    posts)."""
    total = sum(r["amount"] for r in overdue)
    lines = [
        f"⏳ بدهی‌های قدیمی (بیش از {DEBT_NUDGE_MIN_DAYS:.0f} روز) — {len(overdue)} نفر، جمع {round(total):,} تومان",
        "برای ثبت پرداخت روی بدهکار بزنید (قدیمی‌ترین اول):",
    ]
    if len(overdue) > MAX_NUDGE_BUTTONS:
        lines.append(f"و {len(overdue) - MAX_NUDGE_BUTTONS} نفر دیگر — فهرست کامل در پنل > Finance.")
    keyboard: list[list[dict]] = []
    row: list[dict] = []
    for r in overdue[:MAX_NUDGE_BUTTONS]:
        name = r["name"] if len(r["name"]) <= _MAX_BUTTON_NAME else r["name"][:_MAX_BUTTON_NAME - 1] + "…"
        row.append({"text": f"{name} · {round(r['amount']):,}",
                    "callback_data": f"debtnudge:{r['customer_id']}"})
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return "\n".join(lines), {"inline_keyboard": keyboard}


def collect_overdue() -> list[dict]:
    """Every customer with real posted debt outstanding for at least
    DEBT_NUDGE_MIN_DAYS, sorted oldest first — the ONE eligibility
    computation shared by the scheduled send, the manual trigger and the
    Telegram console's list screen (which re-reads it on every render, so
    amounts and cleared debtors are always live, never stale from message
    time)."""
    now = utcnow().replace(tzinfo=None)

    with Session(engine) as session:
        book = MoneyBook(session)
        customers = session.exec(select(Customer)).all()

        overdue: list[dict] = []
        for c in customers:
            posted = book.customer_posted(c)
            if posted <= 0:
                continue
            entries = _customer_ledger_scope(session, c)
            age_days = _debt_age_days(entries, now)
            if age_days is None or age_days < DEBT_NUDGE_MIN_DAYS:
                continue
            overdue.append({"name": c.name, "customer_id": c.id, "amount": posted, "days": round(age_days)})

    overdue.sort(key=lambda r: -r["days"])
    return overdue


async def run_debt_nudge() -> dict:
    """Entry point, called every other day by the scheduler. Sends one
    actionable message for collect_overdue()'s result: a short summary plus
    one button per debtor (oldest debt first) that opens the bot's
    payment-recording console. Nobody eligible means nothing is sent —
    which is the point (no noise)."""
    overdue = collect_overdue()

    if not overdue:
        log.info("Debt nudge: nothing overdue past %.0f days", DEBT_NUDGE_MIN_DAYS)
        return {"sent": False, "count": 0}

    message, markup = _build_nudge(overdue)

    try:
        await notify_admin_with_buttons(message, markup)
    except Exception as exc:
        log.warning("Debt nudge failed to send (will retry next scheduled run): %s", exc)
        return {"sent": False, "count": len(overdue), "error": str(exc)}

    return {"sent": True, "count": len(overdue)}
