"""Monthly payg settlement, tied to the real Persian (Jalali) calendar month
— not a rolling 30-day cycle (which drifts against real month boundaries,
since Jalali months run 29-31 days), and not something the operator has to
remember to click. On the night of the last day of each Jalali month, every
payg group (billed to its representative) and every standalone payg account
gets its accrued usage charged and its billing baseline rolled forward,
exactly like clicking "Settle cycle" would — reusing that same, already
proven settle_group/settle_account logic rather than reimplementing it.

Money-critical, same as sync_job's next-plan auto-queue: the operator is
notified BEFORE anything is charged or reset, and a failed notification
blocks the whole batch for that day. Unlike a single account's near-quota
check (which just retries next cycle, 60 seconds later), this only has one
real trigger point a month — so a bare "did today already work" flag would
let one bad Telegram moment silently skip an entire month. See
_target_settlement_period: the period being settled doesn't advance to the
next month until this one has actually succeeded, so a failure keeps
retrying on every subsequent day this job runs (daily, via the scheduler)
until it does.
"""

import logging
from datetime import datetime, timedelta

import jdatetime
from sqlmodel import Session, select

from app.db import engine
from app.models import Account, AppSettings, BillingMode, Customer, Group, MonthlySettlementBatch, utcnow
from app.notify import notify_admin
from app.routers.accounts import settle_account
from app.routers.groups import _invoice_lines, settle_group
from app.schemas import AccountSettleRequest, GroupSettleRequest
from app.services import GB, billable_bytes, effective_rate, get_settings

log = logging.getLogger(__name__)

_EXCLUDED_STATUSES = ("disabled", "deleted_from_marzban")

_JALALI_MONTHS_FA = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _period_label(year: int, month: int) -> str:
    return f"{_JALALI_MONTHS_FA[month - 1]} {year}"


def _target_settlement_period(today_j: jdatetime.date) -> tuple[int, int]:
    """The most recent Jalali (year, month) whose last day has been reached
    as of today. Always well-defined, and does NOT advance to the next
    month until the current target is marked done in AppSettings — so a
    failed attempt on the actual last day gets retried every day after,
    instead of the whole month being silently skipped."""
    tomorrow_j = today_j + timedelta(days=1)
    if tomorrow_j.month != today_j.month:
        return today_j.year, today_j.month
    first_of_this_month = jdatetime.date(today_j.year, today_j.month, 1)
    last_day_of_prev_month = first_of_this_month - timedelta(days=1)
    return last_day_of_prev_month.year, last_day_of_prev_month.month


def _settlement_message(gb: float, amount: float, period_label: str) -> str:
    """Ready-to-forward text for the customer, matching the tone/shape of
    the existing group invoice copy feature — this is the payg equivalent
    of a monthly usage bill, not a renewal offer."""
    return (
        "سلام و وقت بخیر\n"
        "خوب هستید انشالله\n"
        f"گزارش مصرف {period_label}: {gb:.2f} گیگ\n"
        f"مبلغ این دوره: {amount:g} تومان"
    )


async def maybe_run_monthly_payg_settlement() -> dict:
    """Entry point, called daily by the scheduler. No-ops on every day
    except when there's an unsettled target period to act on."""
    today_j = jdatetime.date.today()
    target_year, target_month = _target_settlement_period(today_j)
    month_key = _month_key(target_year, target_month)

    with Session(engine) as session:
        app_settings = get_settings(session)
        if app_settings.last_payg_monthly_settlement == month_key:
            return {"ran": False, "reason": "already settled", "period": month_key}

    log.info("Running monthly payg settlement for %s", month_key)
    return await _run_monthly_payg_settlement(month_key, target_year, target_month)


async def _run_monthly_payg_settlement(month_key: str, year: int, month: int) -> dict:
    period_label = _period_label(year, month)
    now = utcnow()

    with Session(engine) as session:
        # ── Compute everything first — pure read, no writes yet ──────────
        group_rows: list[dict] = []
        for g in session.exec(select(Group).where(Group.billing_mode == BillingMode.payg)).all():
            accounts = session.exec(select(Account).where(Account.group_id == g.id)).all()
            if not accounts:
                continue
            lines = _invoice_lines(session, accounts, g)
            total = round(sum(line.amount for line in lines), 2)
            total_gb = round(sum(line.billable_gb for line in lines), 2)
            if total <= 0:
                continue
            rep = session.get(Customer, g.representative_customer_id)
            group_rows.append({
                "group_id": g.id,
                "name": rep.name if rep else g.name,
                "gb": total_gb,
                "amount": total,
            })

        account_rows: list[dict] = []
        standalone = session.exec(
            select(Account).where(
                Account.group_id.is_(None),
                Account.billing_mode == BillingMode.payg,
                Account.customer_id.is_not(None),
            )
        ).all()
        for a in standalone:
            if a.status in _EXCLUDED_STATUSES:
                continue
            billable_gb = billable_bytes(a, BillingMode.payg) / GB
            rate = effective_rate(session, a)
            amount = round(billable_gb * rate, 2)
            if amount <= 0:
                continue
            cust = session.get(Customer, a.customer_id)
            account_rows.append({
                "account_id": a.id,
                "name": cust.name if cust else a.marzban_username,
                "gb": round(billable_gb, 2),
                "amount": amount,
            })

    if not group_rows and not account_rows:
        log.info("Monthly payg settlement for %s: nothing to bill, marking done", month_key)
        _mark_period_done(month_key)
        return {"ran": True, "period": month_key, "groups": 0, "accounts": 0, "total": 0.0}

    # ── The gate: this MUST succeed before anything below touches money ──
    grand_total = round(sum(r["amount"] for r in group_rows) + sum(r["amount"] for r in account_rows), 2)
    summary_lines = [f"📅 تسویه ماهانه payg — {period_label}", ""]
    if group_rows:
        summary_lines.append("گروه‌ها:")
        summary_lines += [f"• {r['name']}: {r['gb']:g} گیگ — {r['amount']:g} تومان" for r in group_rows]
        summary_lines.append("")
    if account_rows:
        summary_lines.append("اکانت‌های مستقل:")
        summary_lines += [f"• {r['name']}: {r['gb']:g} گیگ — {r['amount']:g} تومان" for r in account_rows]
        summary_lines.append("")
    summary_lines.append(f"جمع کل: {grand_total:g} تومان — {len(group_rows) + len(account_rows)} مورد")
    summary_lines.append("")
    summary_lines.append("الان شارژ و ریست هرکدوم انجام میشه؛ پیام آماده برای فوروارد هرکدوم جدا میاد.")
    summary_lines.append("برای ثبت پرداخت هرکدوم، پنل وب > تسویه‌های ماهانه.")

    await notify_admin("\n".join(summary_lines))

    # ── Gate passed — now actually settle everyone, one failure at a time ──
    settled = 0
    failed: list[str] = []

    with Session(engine) as session:
        for r in group_rows:
            try:
                await settle_group(r["group_id"], GroupSettleRequest(mark_paid=False), session)
                session.add(MonthlySettlementBatch(
                    jalali_period=month_key, group_id=r["group_id"], display_name=r["name"],
                    billable_gb=r["gb"], amount=r["amount"], settled_at=now,
                ))
                session.commit()
                settled += 1
            except Exception as exc:
                session.rollback()
                log.error("Monthly payg settle failed for group %s: %s", r["name"], exc)
                failed.append(r["name"])
                continue
            try:
                await notify_admin(_settlement_message(r["gb"], r["amount"], period_label))
            except Exception as exc:
                log.warning("Settled group %s but its forward-ready message failed to send: %s", r["name"], exc)

        for r in account_rows:
            try:
                await settle_account(r["account_id"], AccountSettleRequest(mark_paid=False), session)
                session.add(MonthlySettlementBatch(
                    jalali_period=month_key, account_id=r["account_id"], display_name=r["name"],
                    billable_gb=r["gb"], amount=r["amount"], settled_at=now,
                ))
                session.commit()
                settled += 1
            except Exception as exc:
                session.rollback()
                log.error("Monthly payg settle failed for account %s: %s", r["name"], exc)
                failed.append(r["name"])
                continue
            try:
                await notify_admin(_settlement_message(r["gb"], r["amount"], period_label))
            except Exception as exc:
                log.warning("Settled account %s but its forward-ready message failed to send: %s", r["name"], exc)

    if failed:
        try:
            await notify_admin(
                f"⚠️ {len(failed)} مورد از تسویه ماهانه {period_label} شکست خورد و بی‌حساب موند "
                f"(ماه بعد دوباره حساب میشه، جمع میشه با این دوره): {'، '.join(failed)}"
            )
        except Exception:
            pass

    _mark_period_done(month_key)
    return {"ran": True, "period": month_key, "groups": len(group_rows), "accounts": len(account_rows),
            "settled": settled, "failed": len(failed), "total": grand_total}


def _mark_period_done(month_key: str) -> None:
    with Session(engine) as session:
        app_settings = get_settings(session)
        app_settings.last_payg_monthly_settlement = month_key
        session.add(app_settings)
        session.commit()
