import logging
import math
import time
from datetime import datetime

import httpx
from sqlmodel import Session, select

from app.config import settings
from app.db import engine
from app.marzban_client import marzban_client
from app.models import Account, AccountEvent, Customer, LedgerEntry, LedgerSource, LedgerType, OnlineSnapshot, QueuedPlan, QueuedPlanStatus, utcnow
from app.services import GB, billable_bytes, effective_billing_mode, effective_rate, monthly_avg_usage

PAGE_SIZE = 200

# How close to running out (GB remaining) before a next plan is queued
# automatically and the operator is notified — matches the manual habit this
# replaces: screenshotting an account once it's down to "about 1GB" and
# messaging the customer by hand.
NEAR_QUOTA_AUTO_QUEUE_REMAINING_GB = 1.0
# The customer-facing message this queues talks about "last month's average"
# — not tied to any group's billing_cycle_days (prepay doesn't use that
# field at all; see groups.py's is_due gating).
AUTO_NEXT_PLAN_DURATION_DAYS = 31


def _round_down_to_multiple_of_5(gb: float, minimum: float = 5.0) -> float:
    """Always a round package size (65 GB, never 65.8) that reads as a real
    plan, rounded DOWN so this never queues more than what was actually
    observed — and never below `minimum`, since a plan rounded to 0 isn't a
    valid one at all (NextPlanRequest requires data_limit_gb > 0)."""
    return max(minimum, math.floor(gb / 5) * 5)


def _renewal_forward_message(gb: float) -> str:
    """The exact customer-facing text an operator was typing by hand for
    every near-quota account, gb substituted in — meant to be copied
    straight out of the Telegram notification below and forwarded as-is."""
    return (
        "سلام و وقت بخیر\n"
        "خوب هستید انشالله\n"
        f"آخرای حجم اشتراکتون هست، من {gb:g} گیگ معادل میانگین مصرف ماه گذشته "
        "براتون شارژ کردم که به محض اتمام این اشتراک فعال بشه\n"
        "اگه کم و زیاده بفرمایید تغییرش بدم"
    )


async def _notify_admin(text: str) -> None:
    """Raises on any failure to send — NOT best-effort, deliberately, for
    the call site that gates a real state change on it (queuing a next
    plan: see _maybe_auto_queue_next_plan below). An operator who never
    sees the notification never gets the chance to review or reprice the
    auto-picked GB before it silently activates later — the account effect
    is indistinguishable from just handing out free data with nobody aware
    it happened. run_sync's own per-account try/except is what stops a
    failure here from taking the rest of sync down with it; that's the
    right place for this to be swallowed, not silently inside this
    function where a caller that DOES need to know would never find out."""
    if not settings.bot_token or not settings.bot_admin_chat_id:
        raise RuntimeError("BOT_TOKEN/BOT_ADMIN_CHAT_ID not set — nowhere to send this notification")
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{settings.bot_token}/sendMessage",
            data={"chat_id": settings.bot_admin_chat_id, "text": text},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Telegram rejected admin notification ({resp.status_code}): {resp.text}")


async def _maybe_auto_queue_next_plan(session: Session, account: Account, now: datetime) -> None:
    """When an account is down to about NEAR_QUOTA_AUTO_QUEUE_REMAINING_GB
    of its package, queue a next plan sized at its own observed monthly
    average (see services.monthly_avg_usage — the same figure the dashboard
    shows, rounded down to a multiple of 5) and notify the operator with a
    ready-to-forward message. Runs independent of status ("limited"/
    "expired") — the whole point is catching this BEFORE the account
    actually runs out, while it's still active.

    Never repeats for the same shortage: skipped entirely the moment a
    pending plan exists, whether this queued it moments ago or the operator
    queued one by hand — same gate the dashboard's "Next ▸" badge reads."""
    if account.data_limit is None:
        return  # unlimited package — "running out" doesn't apply
    remaining_gb = (account.data_limit - account.used_traffic) / GB
    if remaining_gb > NEAR_QUOTA_AUTO_QUEUE_REMAINING_GB:
        return

    already_queued = session.exec(
        select(QueuedPlan).where(
            QueuedPlan.account_id == account.id,
            QueuedPlan.status == QueuedPlanStatus.pending,
        )
    ).first()
    if already_queued:
        return

    avg_gb, _confidence, _observed_days = monthly_avg_usage(account, now.replace(tzinfo=None))
    if avg_gb is None:
        # Not enough observed history for a trustworthy average — surfaced
        # as a heads-up so the operator still finds out, but no plan is
        # guessed at (same "insufficient_data" principle the dashboard
        # itself follows rather than showing a number from too little data).
        await _notify_admin(
            f"⚠️ اکانت «{account.marzban_username}» {remaining_gb:.2f} گیگ مونده، ولی هنوز داده‌ی کافی "
            "برای میانگین مصرف نداریم — پلن بعدی رو دستی تنظیم کن."
        )
        return

    queue_gb = _round_down_to_multiple_of_5(avg_gb)

    # Notify BEFORE writing anything — if this raises (Telegram down, bot
    # misconfigured, whatever), nothing below runs and nothing gets added
    # to the session for this account. A plan the operator was never told
    # about would still activate on schedule regardless — this is the same
    # "external step succeeds first, local write only happens after"
    # ordering _activate_next_plan already uses for the exact same reason.
    await _notify_admin(
        f"🔔 اکانت «{account.marzban_username}» {remaining_gb:.2f} گیگ مونده — پلن بعدی خودکار ثبت شد: "
        f"{queue_gb:g} گیگ / {AUTO_NEXT_PLAN_DURATION_DAYS} روز (میانگین ماه گذشته: {avg_gb:g} گیگ).\n\n"
        "پیام آماده برای فوروارد به مشتری:\n"
        "———\n"
        f"{_renewal_forward_message(queue_gb)}\n"
        "———"
    )

    session.add(QueuedPlan(account_id=account.id, data_limit_gb=queue_gb, duration_days=AUTO_NEXT_PLAN_DURATION_DAYS))
    session.add(AccountEvent(
        account_id=account.id,
        action="next_plan_auto_queued",
        detail=f"Auto-queued {queue_gb:g} GB / {AUTO_NEXT_PLAN_DURATION_DAYS} days "
        f"({remaining_gb:.2f} GB remaining, monthly avg {avg_gb:g} GB)",
        date=now,
        source=LedgerSource.sync,
    ))

# An account counts as "currently online" if Marzban reported a connection
# within this many seconds of sync running. Marzban doesn't expose a live
# online/offline flag directly — only online_at, a last-seen timestamp — so
# this threshold is this dashboard's own definition, not Marzban's. 3 minutes
# comfortably covers normal client check-in intervals without counting
# someone who disconnected minutes ago as still online.
ONLINE_THRESHOLD_SECONDS = 180


def _parse_online_at(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        # Marzban returns ISO 8601; strip a trailing Z if present since SQLite/
        # our own datetimes are stored naive-UTC throughout this codebase.
        return datetime.fromisoformat(str(value).replace("Z", "")).replace(tzinfo=None)
    except ValueError:
        return None


async def _fetch_all_marzban_users() -> list[dict]:
    users: list[dict] = []
    offset = 0
    while True:
        page = await marzban_client.list_users(offset=offset, limit=PAGE_SIZE)
        batch = page.get("users", [])
        users.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return users


async def _activate_next_plan(session: Session, account: Account, plan: QueuedPlan, now) -> None:
    """Activate a queued next plan for an account whose current plan just ended.

    This is the money-critical function. Order of operations matters:
      1. Work out what the OLD plan still owes, from the account's current
         (pre-reset) state — every field that feeds this is overwritten below
      2. Call Marzban: modify_user (new limits + status=active) + reset_user
         (zero used_traffic)
      3. ONLY once Marzban has accepted both calls, write anything locally:
         the old plan's charge, the new Account state, the plan's activated
         status, and an AccountEvent for the audit trail
      4. Commit, so the local record is durably paired with a Marzban change
         that cannot be rolled back

    Marzban comes BEFORE any session.add() deliberately. run_sync catches and
    logs a failure here so one bad account can't abort the whole sync — which
    means anything already added to the session survives to the end-of-sync
    commit anyway. Posting the charge first and then failing on modify_user
    would therefore COMMIT that charge while leaving the plan 'pending', and
    the next sync cycle would charge for the same ended plan again, once per
    cycle, indefinitely. Writing only after Marzban succeeds means a failure
    leaves nothing behind to roll back: the plan stays pending and the retry
    is clean.

    The commit at the end is for the opposite hazard: once Marzban has reset
    the account there is no undo, so if a later part of run_sync raised and
    took this activation down with it, Marzban would report the account
    'active' on the next cycle, the limited/expired guard would never fire
    again, and the ended plan's usage would go unbilled forever.
    """
    log = logging.getLogger(__name__)

    # Step 1: what the old plan still owes, from the CURRENT (pre-reset) state.
    old_mode = effective_billing_mode(session, account)
    old_billable = billable_bytes(account, old_mode)
    old_rate = effective_rate(session, account)
    old_amount = round((old_billable / GB) * old_rate, 2)

    # Step 2: Call Marzban API — new limits + reactivate + reset usage
    new_data_limit = round(plan.data_limit_gb * GB)
    new_expire = int(time.time()) + plan.duration_days * 86400

    await marzban_client.modify_user(account.marzban_username, {
        "data_limit": new_data_limit,
        "expire": new_expire,
        "status": "active",
    })
    await marzban_client.reset_user(account.marzban_username)

    # Step 3: Marzban accepted the new plan — record it locally.
    # Charged on account_id alone, with no `customer_id is not None` guard:
    # MoneyBook attributes an entry with account_id set to that account
    # regardless of customer_id (see its "ONE OWNER PER ENTRY" header), so
    # skipping the charge for an unassigned account would reset its usage to
    # zero while silently billing nobody for the plan that just ended.
    if old_amount > 0:
        session.add(LedgerEntry(
            type=LedgerType.charge,
            amount=old_amount,
            customer_id=account.customer_id,
            group_id=account.group_id,
            account_id=account.id,
            note=f"Auto-settled: plan ended ({now.date().isoformat()}), next plan activating",
            source=LedgerSource.sync,
        ))
        log.info("Auto-settled %s: charged %.2f for ended plan", account.marzban_username, old_amount)

    account.data_limit = new_data_limit
    account.expire = new_expire
    account.used_traffic = 0
    account.status = "active"
    # None means keep whatever billing_mode the account has right now — not
    # whatever it was when the plan was queued, since an operator could have
    # changed it via BillingSection in the meantime.
    if plan.billing_mode is not None:
        account.billing_mode = plan.billing_mode
    account.usage_baseline = 0
    account.usage_baseline_at = now
    account.billed_data_limit = 0
    account.last_synced_at = now
    session.add(account)

    plan.status = QueuedPlanStatus.activated
    plan.activated_at = now
    session.add(plan)

    mode_note = f" | switched to {plan.billing_mode.value}" if plan.billing_mode else ""
    session.add(AccountEvent(
        account_id=account.id,
        action="next_plan_activated",
        detail=f"Auto-activated: {plan.data_limit_gb} GB / {plan.duration_days} days"
        f" (old plan billed {old_amount}){mode_note}",
        date=now,
        source=LedgerSource.sync,
    ))

    # Step 4: pair the local record durably with the irreversible Marzban call.
    session.commit()

    log.info("Activated next plan for %s: %.1f GB / %d days", account.marzban_username, plan.data_limit_gb, plan.duration_days)


async def run_sync() -> dict:
    """Pulls every user from Marzban and mirrors usage/status/limits into the
    local Account table. Every account belongs to one person by default — a
    Group is the deliberate exception for when several accounts are meant to
    be billed together, not the norm — so a Marzban user with no local match
    yet gets its own personal Customer (named after the Marzban username)
    created and linked immediately, rather than being left unowned until an
    operator manually assigns one. Sync never touches group_id, or
    customer_id on an account that already has one — both stay exclusively
    an operator action from that point on."""
    marzban_users = await _fetch_all_marzban_users()
    now = utcnow()
    created = 0
    updated = 0
    activated_plans = 0

    with Session(engine) as session:
        existing = {a.marzban_username: a for a in session.exec(select(Account)).all()}
        touched: list[Account] = []
        pending_customers: list[tuple[Account, Customer]] = []

        for mu in marzban_users:
            username = mu["username"]
            account = existing.get(username)
            if account is None:
                account = Account(marzban_username=username)
                # Every account is its own billing identity by default — a
                # Group is the deliberate, occasional exception, not
                # something an operator has to opt every account out of.
                # Named after the Marzban username as a starting point;
                # rename it from the Customers page like any other customer.
                personal_customer = Customer(name=username)
                session.add(personal_customer)
                pending_customers.append((account, personal_customer))

                # first_seen_traffic baselines the MONTHLY-AVERAGE-USAGE
                # ESTIMATE (a display figure) at this account's lifetime total
                # right now — averaging in months of pre-existing history
                # would produce a nonsensical inflated rate, since that usage
                # didn't happen "recently."
                lifetime = mu.get("lifetime_used_traffic", 0)
                account.first_seen_traffic = lifetime
                account.first_seen_traffic_at = now

                # usage_baseline is a DIFFERENT thing: what BILLING is measured
                # from. Deliberately left at the model default (0), NOT set to
                # `lifetime` — a reseller onboarding an existing Marzban
                # install has real, unpaid usage on day one, and the whole
                # point of this dashboard is to make that visible as debt, not
                # hide it because it predates the first sync that happened to
                # notice the account. (An earlier version of this code set it
                # to `lifetime` here on the "don't double-bill" theory that
                # pre-existing usage might already have been paid for outside
                # the system — in practice this made real debt disappear by
                # default, the opposite of what a billing system should do.)
                created += 1
            else:
                updated += 1

            # Captured before overwriting, for the external-change check below.
            # A change made THROUGH this dashboard's own Adjust endpoint is
            # never visible here: that endpoint already writes the new value
            # immediately, so by the time sync runs next, old == new and
            # nothing fires. Anything sync itself detects as a diff therefore
            # happened somewhere sync doesn't control — i.e. directly in
            # Marzban — which is exactly the "resilience to out-of-band
            # changes" gap: this doesn't bill for it (there's no agreed price
            # to infer), but it makes it visible in the account's History so
            # the operator can see it happened and decide whether to invoice.
            prev_data_limit = account.data_limit
            prev_expire = account.expire
            prev_used_traffic = account.used_traffic

            account.used_traffic = mu.get("used_traffic", 0)
            account.lifetime_used_traffic = mu.get("lifetime_used_traffic", 0)
            account.data_limit = mu.get("data_limit")
            account.expire = mu.get("expire")
            account.status = mu.get("status")
            account.online_at = _parse_online_at(mu.get("online_at"))
            account.last_synced_at = now
            session.add(account)
            touched.append(account)

            if account.id is not None:
                # Only compare when BOTH values are real numbers — None means
                # "unlimited"/"never expires" in Marzban's own semantics, not
                # zero, so a None-involved transition is a plan CHANGE (e.g.
                # limited -> unlimited), not a comparable "increase," and
                # treating None as 0 would produce a nonsense multi-year delta.
                if prev_data_limit is not None and account.data_limit is not None and prev_data_limit < account.data_limit:
                    added_gb = (account.data_limit - prev_data_limit) / (1024**3)
                    session.add(
                        AccountEvent(
                            account_id=account.id,
                            action="external_data_limit_increase",
                            detail=f"+{added_gb:.2f} GB added outside this dashboard (in Marzban directly)",
                            date=now,
                            source=LedgerSource.sync,
                        )
                    )
                if prev_expire is not None and account.expire is not None and prev_expire < account.expire:
                    added_days = (account.expire - prev_expire) / 86400
                    session.add(
                        AccountEvent(
                            account_id=account.id,
                            action="external_expire_extend",
                            detail=f"+{added_days:.1f} days added outside this dashboard (in Marzban directly)",
                            date=now,
                            source=LedgerSource.sync,
                        )
                    )

                # Billing is based on used_traffic (see models.py's Account.usage_baseline),
                # which assumes resets only ever happen through this dashboard's own
                # reset/settle endpoints — those update used_traffic immediately, so
                # sync never sees a "surprise" drop from its own actions. A drop sync
                # DOES see therefore means the account was reset directly in Marzban,
                # outside this dashboard entirely. Whatever had accrued since the last
                # baseline is now unrecoverable from Marzban's own data (used_traffic
                # already reflects the new, post-reset cycle) — this can't be backfilled
                # as a charge (no way to know if the operator already collected for it
                # by some other means), but it's surfaced here rather than silently
                # vanishing, and the baseline is snapped forward so future accrual is
                # tracked correctly from this point on instead of going permanently
                # negative (clamped to 0 forever, silently under-billing every cycle
                # after this one too).
                if prev_used_traffic > account.used_traffic:
                    unbilled_gb = max(0, prev_used_traffic - account.usage_baseline) / (1024**3)
                    detail = "Usage was reset outside this dashboard (directly in Marzban)."
                    if unbilled_gb > 0:
                        detail += f" ~{unbilled_gb:.2f} GB was accrued but not yet billed at that point — invoice manually if needed."
                    session.add(
                        AccountEvent(
                            account_id=account.id,
                            action="external_usage_reset",
                            detail=detail,
                            date=now,
                            source=LedgerSource.sync,
                        )
                    )
                    account.usage_baseline = account.used_traffic
                    account.usage_baseline_at = now

                # ── Next-plan auto-activation ──────────────────────────────
                # If the account just ended (limited or expired) and has a
                # queued plan waiting, activate it: bill the old plan, call
                # Marzban to set new limits + reset usage, update local state.
                # Runs INSIDE the per-account if-block (account.id is not None)
                # so newly-created accounts (id is None before flush) are
                # never candidates — they can't have a QueuedPlan yet.
                if account.status in ("limited", "expired"):
                    pending_plan = session.exec(
                        select(QueuedPlan).where(
                            QueuedPlan.account_id == account.id,
                            QueuedPlan.status == QueuedPlanStatus.pending,
                        )
                    ).first()
                    if pending_plan:
                        try:
                            await _activate_next_plan(session, account, pending_plan, now)
                            activated_plans += 1
                        except Exception as exc:
                            logging.getLogger(__name__).error(
                                "Failed to activate next plan for %s: %s",
                                account.marzban_username, exc,
                            )

                # ── Auto-queue a next plan while there's still time ────────
                # Deliberately NOT nested inside the limited/expired check
                # above — the whole point is catching an account BEFORE it
                # runs out, while it's still active, not after.
                try:
                    await _maybe_auto_queue_next_plan(session, account, now)
                except Exception as exc:
                    logging.getLogger(__name__).error(
                        "Failed to auto-queue next plan for %s: %s",
                        account.marzban_username, exc,
                    )

        # ── Soft-delete detection ──────────────────────────────────────
        # Any local account whose marzban_username was NOT in the list
        # Marzban just returned has been deleted there. Mark it locally
        # so the UI can distinguish "genuinely gone" from "active" without
        # losing ledger history (hard-deleting the row would orphan entries).
        # Idempotent: only fires once per account, not on every sync.
        marzban_usernames = {mu["username"] for mu in marzban_users}
        deleted = 0
        for username, account in existing.items():
            if username not in marzban_usernames and account.status != "deleted_from_marzban":
                account.status = "deleted_from_marzban"
                account.last_synced_at = now
                session.add(account)
                if account.id is not None:
                    session.add(
                        AccountEvent(
                            account_id=account.id,
                            action="deleted_from_marzban",
                            detail="This user was deleted directly in Marzban. Marked locally — ledger history preserved.",
                            date=now,
                            source=LedgerSource.sync,
                        )
                    )
                deleted += 1

        # Recorded as a side effect of this sync, not a separate poller — a
        # dedicated online-count poller would mean extra Marzban logins/
        # requests on top of what sync already makes, working directly against
        # the "don't request more than necessary" goal. Trend granularity is
        # therefore exactly the sync interval. Counted from `touched` (every
        # account Marzban reported just now), not `existing` — that dict was
        # built before this loop and never gained the ones just created here.
        session.flush()
        for acc, cust in pending_customers:
            acc.customer_id = cust.id

        now_naive = now.replace(tzinfo=None)
        online_count = sum(
            1
            for a in touched
            if a.online_at is not None and (now_naive - a.online_at).total_seconds() <= ONLINE_THRESHOLD_SECONDS
        )
        session.add(OnlineSnapshot(recorded_at=now, online_count=online_count, total_accounts=len(touched)))

        session.commit()

    return {"marzban_user_count": len(marzban_users), "created": created, "updated": updated, "deleted": deleted, "activated_plans": activated_plans, "synced_at": now}
