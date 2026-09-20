import logging
import time
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.auth import require_auth
from app.bulk_accounts import (
    DeliveryItem,
    build_caption,
    deliver_bulk_qr_messages,
    format_plan_line,
    plan_bulk_usernames,
    resolve_subscription_url,
)
from app.config import settings
from app.db import get_session
from app.marzban_client import MarzbanAuthError, MarzbanUnavailable, marzban_client
from app.models import Account, AccountEvent, BillingMode, Customer, Group, LedgerEntry, LedgerSource, LedgerType, QueuedPlan, QueuedPlanStatus, RateChange, utcnow
from app.schemas import (
    AccountAdjustRequest,
    AccountBillingUpdate,
    AccountCreateRequest,
    AccountEventRead,
    AccountRead,
    AccountRelationshipUpdate,
    AccountResetRequest,
    AccountRow,
    AccountSettleRequest,
    BulkAccountCreateRequest,
    BulkAccountCreateResult,
    BulkAccountItem,
    BulkAccountPlannedName,
    BulkAccountPreview,
    NextPlanRead,
    NextPlanRequest,
)
from app.services import (
    GB,
    PAYG_DEFAULT_DATA_LIMIT_GB,
    account_posted_balance,
    attributable_consumed_gb,
    billable_bytes,
    bytes_from_gb,
    cancel_pending_queued_plan,
    close_out_payg_usage_before_delete,
    effective_billing_mode,
    effective_rate,
    enrich_accounts,
    roll_payg_baseline_after_reset,
    serialise_billing,
    sync_marzban_fields,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/accounts", tags=["accounts"], dependencies=[Depends(require_auth)])

SECONDS_IN_DAY = 86400

@router.get("", response_model=list[AccountRow])
def list_accounts(
    unassigned_only: bool = False,
    customer_id: Optional[int] = None,
    group_id: Optional[int] = None,
    offset: int = 0,
    limit: Optional[int] = None,
    session: Session = Depends(get_session),
):
    """
    List accounts with optional filtering by assignment status, customer, or group.

    Args:
        unassigned_only (bool): If True, returns only accounts not assigned to any customer or group.
        customer_id (Optional[int]): Filter accounts belonging to a specific customer.
        group_id (Optional[int]): Filter accounts belonging to a specific group.
        offset (int): Pagination offset.
        limit (Optional[int]): Maximum number of records to return. Unbounded by
            default — the frontend doesn't paginate this list, so a default cap
            here would silently hide accounts past it on every screen.
        session (Session): Database session.

    Returns:
        list[AccountRow]: A list of enriched account records.
    """
    # Soft-deleted (see models.py's Account.deleted_at) accounts never show
    # here — this is the operator's live fleet view, not a history browser.
    # Ledger drill-down still works: get_account and every ledger query
    # below look up by id directly, unfiltered, so a deleted account's past
    # charges remain reachable from wherever they're referenced.
    stmt = select(Account).where(Account.deleted_at.is_(None))
    if unassigned_only:
        stmt = stmt.where(Account.customer_id.is_(None), Account.group_id.is_(None))
    if customer_id is not None:
        stmt = stmt.where(Account.customer_id == customer_id)
    if group_id is not None:
        stmt = stmt.where(Account.group_id == group_id)
    stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    accounts = session.exec(stmt).all()
    return enrich_accounts(session, accounts)


@router.get("/{account_id}", response_model=AccountRow)
def get_account(account_id: int, session: Session = Depends(get_session)):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    return enrich_accounts(session, [account])[0]


@router.post("", response_model=AccountRead)
async def create_account(body: AccountCreateRequest, session: Session = Depends(get_session), operator: str = Depends(require_auth)):
    """
    Create a new account locally and in Marzban.

    This endpoint validates customer and group relationships, constructs the required
    payload, and provisions the user in Marzban. On success, it creates a local
    Account record and logs a creation event.

    Args:
        body (AccountCreateRequest): The details required to create a new account.
        session (Session): Database session.

    Raises:
        HTTPException(404): If the provided customer_id or group_id does not exist.
        HTTPException(400): If Marzban rejects the payload or the username already exists locally.
        HTTPException(502): If Marzban is unavailable or authentication fails.

    Returns:
        AccountRead: The newly created account record.
    """

    if body.customer_id is not None and not session.get(Customer, body.customer_id):
        raise HTTPException(404, "customer_id not found")
    if body.group_id is not None and not session.get(Group, body.group_id):
        raise HTTPException(404, "group_id not found")

    marzban_payload = {
        "username": body.marzban_username,
        "proxies": body.proxies if body.proxies is not None else settings.marzban_default_proxies,
        "inbounds": body.inbounds if body.inbounds is not None else settings.marzban_default_inbounds,
        "expire": body.expire,
        "data_limit": body.data_limit,
        "data_limit_reset_strategy": body.data_limit_reset_strategy,
        "status": body.status,
        "note": body.note,
    }

    try:
        marzban_user = await marzban_client.create_user(marzban_payload)
    except ValueError as exc:
        raise HTTPException(400, f"Marzban rejected this user: {exc}")
    except (MarzbanUnavailable, MarzbanAuthError) as exc:
        raise HTTPException(502, str(exc))

    now = utcnow()
    account = Account(
        marzban_username=body.marzban_username,
        customer_id=body.customer_id,
        group_id=body.group_id,
        role=body.role,
        rate_per_gb=body.rate_per_gb,
        used_traffic=marzban_user.get("used_traffic", 0),
        lifetime_used_traffic=marzban_user.get("lifetime_used_traffic", 0),
        first_seen_traffic=marzban_user.get("lifetime_used_traffic", 0),
        first_seen_traffic_at=now,
        # This account was just created via marzban_client.create_user() above,
        # so its lifetime usage is always genuinely 0 here — usage_baseline is
        # deliberately left at the model default (0) rather than mirrored from
        # lifetime, matching sync_job.py's policy (see its comment): billing
        # should never start from "whatever Marzban already reports," only
        # from real observed usage.
        usage_baseline_at=now,
        data_limit=marzban_user.get("data_limit"),
        expire=marzban_user.get("expire"),
        status=marzban_user.get("status"),
        last_synced_at=now,
    )
    try:
        session.add(account)
        session.commit()
        session.refresh(account)

        session.add(AccountEvent(account_id=account.id, action="create", detail="Created via dashboard", created_by=operator))
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(400, "This marzban_username is already tracked locally")
    except Exception:
        session.rollback()
        raise

    return account



# ── Bulk ("family") account creation ──────────────────────────────────────
#
# Blast radius: MEDIUM-HIGH. Each item is an irreversible Marzban create.
# Two rules follow from that and must not be "simplified" away:
#
#   1. ONE COMMIT PER ITEM (AGENTS.md §4.5). A single transaction wrapping
#      the loop would roll the local rows back on a failure at item k while
#      leaving items 1..k-1 live in Marzban — the exact split-brain state
#      reset_group_cycle once produced. Every item is therefore durable the
#      moment it succeeds, and the response reports per-item outcomes rather
#      than one all-or-nothing status.
#
#   2. NOTIFICATIONS NEVER GATE CREATION. Elsewhere in this codebase a failed
#      Telegram send deliberately blocks the action (see notify.py) — because
#      there the action is a charge the operator must get a chance to review.
#      Here the action is already irreversible by the time any message could
#      be sent, so blocking on delivery would buy nothing and lose the links.
#      Sends run as a background task and report their own failures.


async def _collect_taken_usernames(session: Session) -> set[str]:
    """Every username already in use, from BOTH sides.

    Local rows alone are not enough: a Marzban user this dashboard has never
    synced is invisible locally, so a local-only check would happily plan a
    name Marzban rejects, and the operator would discover it as a mid-batch
    failure instead of an up-front skip.

    One call for the whole batch, never one per item — see
    MarzbanClient.list_all_users.
    """
    local = {row for row in session.exec(select(Account.marzban_username)).all() if row}
    marzban_users = await marzban_client.list_all_users()
    remote = {u.get("username") for u in marzban_users if u.get("username")}
    return local | remote


def _validate_bulk_relations(body: BulkAccountCreateRequest, session: Session) -> None:
    if body.customer_id is not None and not session.get(Customer, body.customer_id):
        raise HTTPException(404, "customer_id not found")
    if body.group_id is not None and not session.get(Group, body.group_id):
        raise HTTPException(404, "group_id not found")


async def _plan_bulk(body: BulkAccountCreateRequest, session: Session):
    _validate_bulk_relations(body, session)
    try:
        taken = await _collect_taken_usernames(session)
    except (MarzbanUnavailable, MarzbanAuthError) as exc:
        # Deliberately fatal before anything is created: without Marzban's own
        # user list the name plan would be based on local rows only, and could
        # collide with users this dashboard has never seen.
        raise HTTPException(502, f"Could not read the existing user list from Marzban: {exc}")
    try:
        return plan_bulk_usernames(
            base_name=body.base_name,
            count=body.count,
            taken=taken,
            start_index=body.start_index,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/bulk/preview", response_model=BulkAccountPreview)
async def preview_bulk_accounts(body: BulkAccountCreateRequest, session: Session = Depends(get_session)):
    """Exactly which usernames POST /bulk would create, without creating any.

    POST and not GET despite being read-only: it takes the same request body
    as the real endpoint, and any drift between the two would make the preview
    a lie. Sharing one schema is what keeps the shown names and the created
    names the same names.
    """
    plan = await _plan_bulk(body, session)
    return BulkAccountPreview(
        base_name=plan.base_name,
        start_index=plan.start_index,
        names=[
            BulkAccountPlannedName(
                index=n.index, marzban_username=n.username, already_exists=n.already_exists
            )
            for n in plan.names
        ],
        will_create=len(plan.free),
        will_skip=len(plan.taken),
    )


def _ensure_family_customer(session: Session, base_name: str) -> tuple[Customer, bool]:
    """The customer a default (owner-less) bulk batch is attached to: an
    existing customer with exactly this name (case-insensitive — rerunning or
    extending a family must not mint a second «khanevade»), else a new one
    with kind='family'. Flushed, not committed: it lands in the SAME
    transaction as the first account that needs it, so a failed first
    account leaves no empty customer behind. Returns (customer, created)."""
    wanted = base_name.strip().lower()
    for c in session.exec(select(Customer)).all():
        if c.name.strip().lower() == wanted:
            return c, False
    customer = Customer(name=base_name.strip(), kind="family")
    session.add(customer)
    session.flush()
    return customer, True


@router.post("/bulk", response_model=BulkAccountCreateResult)
async def create_bulk_accounts(
    body: BulkAccountCreateRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    operator: str = Depends(require_auth),
):
    """Creates `count` accounts named base1, base2, … and sends the operator
    one Telegram message per account (QR + subscription link + username).

    Money: this posts NO ledger entry, by design and per the operator's
    explicit decision. It is the same contract as POST /api/accounts — an
    account can be attached to a customer or group here, but what to charge
    for it stays a separate, deliberate action. Do not add automatic billing
    to this endpoint without re-reading AGENTS.md §4.3.
    """
    plan = await _plan_bulk(body, session)

    # Computed ONCE, before the loop, so every account in the batch carries
    # the identical expiry. Computing it per item would make the last account
    # of a slow 50-account batch expire measurably later than the first.
    expire_ts = int(time.time()) + body.expire_days * SECONDS_IN_DAY if body.expire_days else None
    data_limit_bytes = bytes_from_gb(body.data_limit_gb) if body.data_limit_gb else None
    plan_line = format_plan_line(body.data_limit_gb, body.expire_days)

    items: list[BulkAccountItem] = []
    deliveries: list[DeliveryItem] = []
    aborted_reason: Optional[str] = None

    # Default ownership: see BulkAccountCreateRequest.unassigned.
    needs_family = body.customer_id is None and body.group_id is None and not body.unassigned
    owner_customer: Optional[Customer] = session.get(Customer, body.customer_id) if body.customer_id is not None else None

    for planned in plan.names:
        if aborted_reason is not None:
            items.append(BulkAccountItem(
                marzban_username=planned.username,
                status="failed",
                error=f"Not attempted — the batch stopped earlier: {aborted_reason}",
            ))
            continue

        if planned.already_exists:
            items.append(BulkAccountItem(
                marzban_username=planned.username,
                status="skipped_exists",
                error="A user with this name already exists in Marzban or is already tracked here",
            ))
            continue

        marzban_payload = {
            "username": planned.username,
            "proxies": body.proxies if body.proxies is not None else settings.marzban_default_proxies,
            "inbounds": body.inbounds if body.inbounds is not None else settings.marzban_default_inbounds,
            "expire": expire_ts,
            "data_limit": data_limit_bytes,
            "data_limit_reset_strategy": body.data_limit_reset_strategy,
            "status": body.status,
            "note": body.note,
        }

        try:
            marzban_user = await marzban_client.create_user(marzban_payload)
        except ValueError as exc:
            # A 4xx for THIS user (duplicate, bad inbound tag). Specific to one
            # item, so the rest of the batch is still worth attempting.
            items.append(BulkAccountItem(
                marzban_username=planned.username, status="failed", error=str(exc),
            ))
            continue
        except (MarzbanUnavailable, MarzbanAuthError) as exc:
            # The panel itself is down or rejecting our credentials. Every
            # remaining item would fail the same way, so stop rather than
            # hammer a dead panel — and say so, instead of returning a wall of
            # identical errors that hides where the batch actually stopped.
            aborted_reason = str(exc)
            items.append(BulkAccountItem(
                marzban_username=planned.username, status="failed", error=str(exc),
            ))
            continue

        subscription_url = resolve_subscription_url(marzban_user.get("subscription_url"))
        now = utcnow()
        family_created_here = False
        if needs_family and owner_customer is None:
            try:
                owner_customer, family_created_here = _ensure_family_customer(session, body.base_name)
            except Exception:  # noqa: BLE001 — falls through to an unowned row rather than losing the account
                session.rollback()
                logger.exception("Bulk batch '%s': couldn't set up the family customer", body.base_name)
        account = Account(
            marzban_username=planned.username,
            customer_id=owner_customer.id if owner_customer is not None else body.customer_id,
            group_id=body.group_id,
            role=body.role,
            rate_per_gb=body.rate_per_gb,
            used_traffic=marzban_user.get("used_traffic", 0),
            lifetime_used_traffic=marzban_user.get("lifetime_used_traffic", 0),
            first_seen_traffic=marzban_user.get("lifetime_used_traffic", 0),
            first_seen_traffic_at=now,
            # usage_baseline left at the model default (0) — same reasoning as
            # create_account above: this user was created seconds ago, so its
            # real usage is 0, and billing should start from observed usage
            # rather than from whatever Marzban happens to report.
            usage_baseline_at=now,
            data_limit=marzban_user.get("data_limit"),
            expire=marzban_user.get("expire"),
            status=marzban_user.get("status"),
            subscription_url=marzban_user.get("subscription_url"),
            last_synced_at=now,
            auto_renew_enabled=body.auto_renew_enabled,
        )
        try:
            session.add(account)
            # flush, not commit: this assigns account.id so the audit event can
            # reference it, while keeping the account row and its event in ONE
            # transaction. A commit here instead would allow an account with no
            # creation event if the next statement failed.
            session.flush()
            session.add(AccountEvent(
                account_id=account.id,
                action="create",
                detail=f"Created via bulk batch '{body.base_name}' ({plan_line})",
                created_by=operator,
            ))
            session.commit()
            session.refresh(account)
        except Exception as exc:  # noqa: BLE001 — the Marzban user already exists; never swallow silently
            session.rollback()
            if family_created_here:
                # The customer row was in the rolled-back transaction with the
                # account; forget it so the next account creates it again.
                owner_customer = None
            logger.exception(
                "Bulk batch '%s': created %s in Marzban but failed to track it locally",
                body.base_name, planned.username,
            )
            items.append(BulkAccountItem(
                marzban_username=planned.username,
                status="created_untracked",
                subscription_url=subscription_url,
                error=(
                    f"Created in Marzban but NOT saved locally ({exc}). The account is live "
                    f"and usable; the sync job will adopt it on its next pass."
                ),
            ))
            # Still worth sending: the customer's link is valid regardless of
            # whether this dashboard managed to record the row.
            if subscription_url:
                deliveries.append(DeliveryItem(
                    username=planned.username,
                    subscription_url=subscription_url,
                    caption=build_caption(planned.username, subscription_url, plan_line),
                ))
            continue

        items.append(BulkAccountItem(
            marzban_username=planned.username,
            status="created",
            account_id=account.id,
            subscription_url=subscription_url,
        ))
        if subscription_url:
            deliveries.append(DeliveryItem(
                username=planned.username,
                subscription_url=subscription_url,
                caption=build_caption(planned.username, subscription_url, plan_line),
            ))
        else:
            # Recorded here too, not only in the delivery summary: a caller
            # that passed notify=False would otherwise never learn the link
            # is missing.
            logger.warning(
                "Bulk batch '%s': %s has no resolvable subscription link",
                body.base_name, planned.username,
            )

    notifications_queued = bool(
        body.notify and deliveries and settings.bot_token and settings.bot_admin_chat_id
    )
    if notifications_queued:
        # Background, not awaited: 50 photo uploads take minutes, and holding
        # the HTTP response open for them would hit the client's timeout long
        # before finishing — leaving the operator with no record of a batch
        # that did in fact create every account.
        background_tasks.add_task(
            deliver_bulk_qr_messages,
            deliveries,
            batch_label=f"Bulk batch '{body.base_name}'",
        )

    return BulkAccountCreateResult(
        base_name=plan.base_name,
        start_index=plan.start_index,
        requested=body.count,
        created=sum(1 for i in items if i.status in ("created", "created_untracked")),
        skipped=sum(1 for i in items if i.status == "skipped_exists"),
        failed=sum(1 for i in items if i.status == "failed"),
        items=items,
        notifications_queued=notifications_queued,
        aborted_reason=aborted_reason,
        customer_id=owner_customer.id if owner_customer is not None else None,
        customer_name=owner_customer.name if owner_customer is not None else None,
    )


@router.patch("/{account_id}/relationship", response_model=AccountRead)
def update_relationship(account_id: int, body: AccountRelationshipUpdate, session: Session = Depends(get_session), operator: str = Depends(require_auth)):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")

    changes = body.model_dump(exclude_unset=True)
    if "customer_id" in changes and changes["customer_id"] is not None:
        if not session.get(Customer, changes["customer_id"]):
            raise HTTPException(404, "customer_id not found")
    if "group_id" in changes and changes["group_id"] is not None:
        if not session.get(Group, changes["group_id"]):
            raise HTTPException(404, "group_id not found")

    try:
        for field, value in changes.items():
            setattr(account, field, value)
        session.add(account)

        session.add(
            AccountEvent(
                account_id=account.id,
                action="relationship_change",
                detail=str(changes),
                created_by=operator,
            )
        )
        session.commit()
        session.refresh(account)
    except Exception:
        session.rollback()
        raise
    return account


@router.patch("/{account_id}/billing", response_model=AccountRead)
async def update_billing(account_id: int, body: AccountBillingUpdate, session: Session = Depends(get_session), operator: str = Depends(require_auth)):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")

    old_rate = account.rate_per_gb
    old_billing_mode = account.billing_mode
    if body.clear_rate:
        account.rate_per_gb = None
    elif body.rate_per_gb is not None:
        account.rate_per_gb = body.rate_per_gb

    if body.billing_mode is not None:
        account.billing_mode = body.billing_mode

    if body.auto_renew_enabled is not None:
        account.auto_renew_enabled = body.auto_renew_enabled

    # Switching a standalone account to payg applies payg's standard shape —
    # no expiry (there is no plan to "end"; the account just runs until its
    # soft cap, which is payg's own billing rhythm) and a 300GB soft cap —
    # in Marzban FIRST, then mirrored locally. Without this, a converted
    # account kept its old prepay expire and small cap, so it kept "ending
    # its plan" and churning through activations even though it now bills
    # metered usage. Grouped accounts are excluded: their effective mode is
    # the group's, and their limits belong to the group's own management.
    payg_shape_applied = False
    marzban_user = None
    if (
        body.billing_mode == BillingMode.payg
        and old_billing_mode != BillingMode.payg
        and not account.group_id
    ):
        try:
            marzban_user = await marzban_client.modify_user(
                account.marzban_username,
                {"expire": 0, "data_limit": int(PAYG_DEFAULT_DATA_LIMIT_GB * GB)},
            )
        except ValueError as exc:
            raise HTTPException(400, f"Marzban rejected the payg shape: {exc}")
        except (MarzbanUnavailable, MarzbanAuthError) as exc:
            raise HTTPException(502, str(exc))
        if marzban_user.get("expire") not in (0, None):
            raise HTTPException(502, f"Marzban did not accept the no-expiry shape (expire={marzban_user.get('expire')})")
        account.expire = None
        account.data_limit = int(PAYG_DEFAULT_DATA_LIMIT_GB * GB)
        payg_shape_applied = True

    try:
        session.add(account)
        if old_rate != account.rate_per_gb:
            # Structured audit trail for the rate — the overwrite-in-place
            # field alone left "was this rate ever 0?" unanswerable (see
            # models.RateChange). Recorded only when the value actually moved.
            session.add(RateChange(
                scope="account",
                account_id=account.id,
                old_rate=old_rate,
                new_rate=account.rate_per_gb,
                created_by=operator,
            ))
        shape_note = (
            " | payg shape applied: expire cleared, data_limit -> 300GB"
            if payg_shape_applied
            else ""
        )
        session.add(
            AccountEvent(
                account_id=account.id,
                action="billing_change",
                detail=f"rate_per_gb={account.rate_per_gb}, billing_mode={account.billing_mode}, auto_renew_enabled={account.auto_renew_enabled}{shape_note}",
                created_by=operator,
            )
        )
        session.commit()
        session.refresh(account)
    except Exception:
        session.rollback()
        raise
    return account


@router.post("/{account_id}/adjust", response_model=AccountRead)
async def adjust_account(account_id: int, body: AccountAdjustRequest, session: Session = Depends(get_session), operator: str = Depends(require_auth)):
    """
    Adjust an account's data limit or expiration date.

    Supports extending limits/days incrementally or setting absolute values.
    Updates the remote Marzban user first, then synchronizes the local account record,
    and logs the adjustment as an account event.

    Args:
        account_id (int): The ID of the account to adjust.
        body (AccountAdjustRequest): The adjustment parameters (e.g., extend_gb, set_expire).
        session (Session): Database session.

    Raises:
        HTTPException(404): If the local account does not exist.
        HTTPException(400): If no valid operations are provided, or Marzban rejects the change.
        HTTPException(502): If Marzban is unavailable or authentication fails.

    Returns:
        AccountRead: The updated account record.
    """
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")

    payload: dict = {}
    detail_parts: list[str] = []

    # Deltas are relative — a negative extend_days/extend_gb is the documented
    # way to reduce (e.g. the UI's "-7 days" preset). Rejecting negatives here
    # would break that, not add safety.
    if body.set_expire is not None:
        payload["expire"] = body.set_expire
        detail_parts.append(f"set_expire={body.set_expire}")
    elif body.extend_days is not None:
        base = account.expire if account.expire else int(time.time())
        payload["expire"] = base + body.extend_days * SECONDS_IN_DAY
        detail_parts.append(f"extend_days={body.extend_days}")

    if body.set_data_limit_gb is not None:
        payload["data_limit"] = bytes_from_gb(body.set_data_limit_gb)
        detail_parts.append(f"set_data_limit_gb={body.set_data_limit_gb}")
    elif body.extend_gb is not None:
        base = account.data_limit or 0
        payload["data_limit"] = max(0, base + bytes_from_gb(body.extend_gb))
        detail_parts.append(f"extend_gb={body.extend_gb}")

    if not payload:
        raise HTTPException(400, "Provide at least one of extend_days/extend_gb/set_expire/set_data_limit_gb")

    try:
        marzban_user = await marzban_client.modify_user(account.marzban_username, payload)
    except ValueError as exc:
        raise HTTPException(400, f"Marzban rejected this change: {exc}")
    except (MarzbanUnavailable, MarzbanAuthError) as exc:
        raise HTTPException(502, str(exc))

    account.expire = marzban_user.get("expire", account.expire)
    account.data_limit = marzban_user.get("data_limit", account.data_limit)
    account.status = marzban_user.get("status", account.status)
    account.last_synced_at = utcnow()
    try:
        session.add(account)

        session.add(
            AccountEvent(
                account_id=account.id,
                action="adjust",
                detail=", ".join(detail_parts) + (f" | note={body.note}" if body.note else ""),
                created_by=operator,
            )
        )
        session.commit()
        session.refresh(account)
    except Exception:
        session.rollback()
        raise
    return account


@router.get("/{account_id}/events", response_model=list[AccountEventRead])
def get_account_events(account_id: int, limit: int = Query(50, ge=1, le=500), session: Session = Depends(get_session)):
    """The audit trail (adjust/reset/billing/ownership changes) that was being
    written since day one but never exposed — the account inspector's History
    section reads it, merged client-side with this account's ledger entries."""
    if not session.get(Account, account_id):
        raise HTTPException(404, "Account not found")
    stmt = (
        select(AccountEvent)
        .where(AccountEvent.account_id == account_id)
        .order_by(AccountEvent.date.desc(), AccountEvent.id.desc())
        .limit(limit)
    )
    return session.exec(stmt).all()


@router.get("/{account_id}/invoice")
def get_account_invoice(account_id: int, session: Session = Depends(get_session)):
    """Standalone (non-group) preview for one account — what /settle would
    charge right now: usage since the last settle for payg, or the package
    (data_limit) itself for prepay (see services.billable_bytes)."""
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    mode = effective_billing_mode(session, account)
    billable_gb = billable_bytes(account, mode) / (1024**3)
    rate = effective_rate(session, account)
    return {
        "account_id": account_id,
        "since": account.usage_baseline_at if mode == BillingMode.payg else None,
        "billable_gb": round(billable_gb, 3),
        "rate_per_gb": rate,
        "amount": round(billable_gb * rate, 2),
    }


@router.post("/{account_id}/settle")
@serialise_billing
async def settle_account(account_id: int, body: AccountSettleRequest = AccountSettleRequest(), session: Session = Depends(get_session), operator: str = Depends(require_auth)):
    """Charges this standalone account for whatever it currently owes — usage
    since the last settle for payg, or the package (data_limit) itself for
    prepay (see services.billable_bytes) — and rolls the matching baseline
    forward. The one-click "Settle" action: no manual GB/price entry, always
    charges exactly the amount already shown as this account's pending.

    For payg specifically, this also resets the account's actual usage in
    Marzban (not just the local billing baseline) — so the meter really
    reads 0 after being billed, same as the automatic payg cap-hit/monthly
    settlements already do. NOT done for prepay: a prepay package's
    data_limit is what Marzban enforces as the hard cap for the package
    already sold, so zeroing used_traffic mid-package would hand the
    customer a second free allowance of the same package instead of billing
    it — prepay's own baseline is billed_data_limit, untouched by Marzban.

    This POSTS A CHARGE — the debt becomes formal/real, which is why the
    balance goes red afterward if `mark_paid` isn't set: nothing has been
    paid yet, only billed. Pass mark_paid=True when the operator is
    collecting payment in the same moment (the common case) to also post a
    credit that clears whatever is still outstanding after this charge.

    Direct (non-HTTP) callers — the monthly payg job is one — MUST pass an
    explicit `operator` identity: the Depends(require_auth) default only
    resolves on the FastAPI path. Without it, the raw Depends object would
    reach created_by and blow up at commit time, rolling back the whole
    settlement. The guard below turns that into a loud, immediate error."""
    if not isinstance(operator, str):
        raise RuntimeError(
            "settle_account called directly without an explicit operator — "
            "pass operator='system:<job>' (the Depends() default is HTTP-only)")
    account = session.exec(select(Account).with_for_update().where(Account.id == account_id)).first()
    if not account:
        raise HTTPException(404, "Account not found")
    if account.group_id is not None:
        raise HTTPException(400, "This account is billed through its group — use /api/groups/{group_id}/settle")

    mode = effective_billing_mode(session, account)
    billable = billable_bytes(account, mode)
    billable_gb = billable / (1024**3)
    rate = effective_rate(session, account)
    amount = round(billable_gb * rate, 2)

    # A charge with no customer belongs to nobody: it never shows in a
    # balance, never appears on an invoice, and quietly disappears from the
    # money the operator is owed. reset_account already refuses this.
    if amount > 0 and not account.customer_id:
        raise HTTPException(400, "Can't charge an unassigned account — assign it to a customer first")

    # Read BEFORE adding the charge below: this issues a SELECT, which
    # autoflushes pending adds, so reading afterwards would already include
    # the charge and make the intent of this number ambiguous.
    prior_balance = 0.0
    if body.mark_paid:
        prior_balance = account_posted_balance(session, account.id)

    # Marzban call BEFORE any DB write, same ordering as reset_account: if
    # this fails, nothing gets charged either, rather than billing a reset
    # that never actually happened.
    marzban_user = None
    if mode == BillingMode.payg:
        try:
            marzban_user = await marzban_client.reset_user(account.marzban_username)
        except ValueError as exc:
            raise HTTPException(400, f"Marzban rejected this reset: {exc}")
        except (MarzbanUnavailable, MarzbanAuthError) as exc:
            raise HTTPException(502, str(exc))

    now = utcnow()
    cycle_note = (
        f"Package settlement for cycle ending {now.date().isoformat()}"
        if mode == BillingMode.prepay
        else f"Usage settlement for cycle ending {now.date().isoformat()}"
    )
    try:
        if amount > 0:
            # gb_amount: what this charge bills for — billable_gb is the very
            # figure `amount` was computed from. consumed_gb: for payg the
            # metered usage IS the bill; for prepay the package is billed
            # whether or not it's been burned down, so attribute only the
            # accrued-usage slice no earlier charge in this meter epoch
            # already took (see attributable_consumed_gb).
            consumed_gb = round(billable_gb, 3) if mode == BillingMode.payg else attributable_consumed_gb(session, account)
            session.add(
                LedgerEntry(
                    type=LedgerType.charge,
                    amount=amount,
                    customer_id=account.customer_id,
                    account_id=account.id,
                    note=cycle_note,
                    source=LedgerSource.web,
                    date=now,
                    gb_amount=round(billable_gb, 3),
                    consumed_gb=consumed_gb,
                    consumed_amount=round(consumed_gb * rate, 2),
                    created_by=operator,
                )
            )
        if body.mark_paid:
            # Credit whatever is still OUTSTANDING once this charge lands — which
            # is NOT the charge amount:
            #  - an account already carrying a credit (they prepaid, or paid
            #    before the usage was invoiced) would otherwise be handed that
            #    credit a second time: settling a 243,916 charge on someone
            #    230,000 in credit would leave them 230,000 in credit, not settled;
            #  - an account carrying unpaid debt from an earlier cycle must still
            #    be cleared even when this cycle adds nothing to charge, or
            #    "payment received" would silently do nothing while the dialog's
            #    preview promised a settled balance.
            # max(0, ...) because an account still in credit afterwards has
            # nothing left to pay.
            # pay_scope="prior_only": the payment being recorded right now is
            # for OLD debt only — this cycle's own charge (`amount`, just
            # posted above) stays outstanding rather than being silently
            # marked paid alongside it.
            credit_amount = round(max(0.0, prior_balance if body.pay_scope == "prior_only" else prior_balance + amount), 2)
            if credit_amount > 0:
                session.add(
                    LedgerEntry(
                        type=LedgerType.credit,
                        amount=credit_amount,
                        customer_id=account.customer_id,
                        account_id=account.id,
                        note=f"Payment received at settlement ({now.date().isoformat()})",
                        source=LedgerSource.web,
                        created_by=operator,
                    )
                )

        if mode == BillingMode.payg:
            sync_marzban_fields(account, marzban_user)
            roll_payg_baseline_after_reset(account, now)
            account.last_synced_at = now
            session.add(AccountEvent(
                account_id=account.id,
                action="settle_reset",
                detail=f"Usage reset via settle (charged {amount:g})",
                created_by=operator,
            ))
        else:
            account.billed_data_limit = account.data_limit or 0
            session.add(AccountEvent(
                account_id=account.id,
                action="settle_reset",
                detail=f"Package marked billed via settle (charged {amount:g})",
                created_by=operator,
            ))
        session.add(account)
        session.commit()
    except Exception:
        session.rollback()
        raise

    return {"account_id": account_id, "charged_amount": amount, "settled_at": now}


@router.post("/{account_id}/reset", response_model=AccountRead)
@serialise_billing
async def reset_account(account_id: int, body: AccountResetRequest, session: Session = Depends(get_session), operator: str = Depends(require_auth)):
    """Starts a new usage cycle in Marzban. If `charge_amount` is explicitly
    given (including 0, to deliberately skip charging e.g. a comp reset), that
    exact value is posted. Otherwise, for a payg account, the accrued usage is
    computed and charged automatically — resetting always rolls the billing
    baseline forward regardless (usage_baseline for payg, billed_data_limit
    for prepay), so leaving this to silently charge nothing would permanently
    lose that cycle's billing data, and a package charged here can't be
    re-billed by a later settle.

    "payg" here means effective_billing_mode, not the raw field: a member of a
    payg group whose own billing_mode was never explicitly touched (it
    defaults to prepay) is still billed as payg by group settle, so a solo
    reset on that same account needs to agree, or resetting it individually
    would silently skip the auto-charge a group settle would have applied."""
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")

    mode = effective_billing_mode(session, account)
    charge_amount = body.charge_amount
    # Read from the PRE-reset state, before sync_marzban_fields below
    # overwrites used_traffic with the post-reset reading — this is the last
    # chance to attribute the cycle's consumption to the charge posted here.
    billable_gb = billable_bytes(account, mode) / (1024**3)
    pre_reset_consumed_gb = (
        round(billable_gb, 3) if mode == BillingMode.payg else attributable_consumed_gb(session, account)
    )
    pre_reset_rate = effective_rate(session, account)
    if charge_amount is None:
        # Both modes, not just payg. A prepay reset used to post nothing and
        # STILL roll billed_data_limit up to data_limit below — which wrote
        # the unbilled package off: no charge anywhere, and the pending
        # amount gone for good. Charging what is actually pending keeps the
        # rule the docstring states ("resetting never loses billing data");
        # an operator who means to comp it passes charge_amount=0 explicitly.
        charge_amount = round(billable_gb * effective_rate(session, account), 2)

    if charge_amount and charge_amount > 0 and not account.customer_id and not account.group_id:
        raise HTTPException(400, "Can't charge an unassigned account — assign it to a customer first")

    try:
        marzban_user = await marzban_client.reset_user(account.marzban_username)
    except ValueError as exc:
        raise HTTPException(400, f"Marzban rejected this reset: {exc}")
    except (MarzbanUnavailable, MarzbanAuthError) as exc:
        raise HTTPException(502, str(exc))

    now = utcnow()
    try:
        if charge_amount and charge_amount > 0:
            session.add(
                LedgerEntry(
                    type=LedgerType.charge,
                    amount=round(charge_amount, 2),
                    customer_id=account.customer_id,
                    group_id=account.group_id,
                    account_id=account.id,
                    note=body.note or f"Usage reset for cycle ending {now.date().isoformat()}",
                    source=LedgerSource.web,
                    date=now,
                    created_by=operator,
                    # GB is only known when the money was derived from the
                    # account's own billing math (charge_amount omitted). An
                    # operator-entered amount maps to no honest GB figure.
                    gb_amount=round(billable_gb, 3) if body.charge_amount is None else None,
                    # The reset zeroes the meter either way, so the accrued
                    # consumption is attributed even when the operator chose
                    # the amount themselves — otherwise it would vanish
                    # from the consumed sums entirely.
                    consumed_gb=pre_reset_consumed_gb,
                    consumed_amount=round(pre_reset_consumed_gb * pre_reset_rate, 2),
                )
            )

        sync_marzban_fields(account, marzban_user)
        # Reset always rolls the RIGHT baseline forward too, regardless of
        # whether a charge was posted — otherwise a prepay account's pending
        # amount (data_limit - billed_data_limit) stays exactly what it was
        # before the reset even after this posted a real charge for it, and
        # the next settle bills the same package a second time. usage_baseline
        # is what payg reads; billed_data_limit is what prepay reads — rolling
        # only the former (as this used to, unconditionally) was a no-op for
        # prepay's own billing math.
        if mode == BillingMode.payg:
            roll_payg_baseline_after_reset(account, now)
        else:
            account.billed_data_limit = account.data_limit or 0
        account.last_synced_at = now
        session.add(account)

        session.add(
            AccountEvent(
                account_id=account.id,
                action="reset",
                detail=f"charge_amount={charge_amount}" + (f" | note={body.note}" if body.note else ""),
                created_by=operator,
            )
        )
        session.commit()
        session.refresh(account)
    except Exception:
        session.rollback()
        raise
    return account


@router.post("/{account_id}/next-plan", response_model=NextPlanRead)
def set_next_plan(account_id: int, body: NextPlanRequest, session: Session = Depends(get_session), operator: str = Depends(require_auth)):
    """Queue a plan to auto-activate when this account's current plan ends.

    Replaces any existing pending plan for this account (there can only be
    one pending plan at a time). When the sync job detects that Marzban has
    set the account's status to 'limited' or 'expired', it will:
      1. Auto-settle the old plan (post a charge for any unbilled amount)
      2. Call Marzban to apply the new data_limit + expire + reset usage
      3. Update local state and mark this plan as activated
    """
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")

    # Cancel any existing pending plan — one-deep queue.
    existing_pending = session.exec(
        select(QueuedPlan).where(
            QueuedPlan.account_id == account_id,
            QueuedPlan.status == QueuedPlanStatus.pending,
        )
    ).first()
    if existing_pending:
        existing_pending.status = QueuedPlanStatus.cancelled
        session.add(existing_pending)

    plan = QueuedPlan(
        account_id=account_id,
        data_limit_gb=body.data_limit_gb,
        duration_days=body.duration_days,
        billing_mode=body.billing_mode,
    )
    try:
        session.add(plan)
        session.add(
            AccountEvent(
                account_id=account_id,
                action="next_plan_queued",
                detail=f"{body.data_limit_gb} GB / {body.duration_days} days"
                + (f" | switches to {body.billing_mode.value}" if body.billing_mode else ""),
                created_by=operator,
            )
        )
        session.commit()
        session.refresh(plan)
    except Exception:
        session.rollback()
        raise
    return plan


@router.get("/{account_id}/next-plan", response_model=NextPlanRead)
def get_next_plan(account_id: int, session: Session = Depends(get_session)):
    """Returns the current pending next plan for this account, or 404."""
    if not session.get(Account, account_id):
        raise HTTPException(404, "Account not found")
    plan = session.exec(
        select(QueuedPlan).where(
            QueuedPlan.account_id == account_id,
            QueuedPlan.status == QueuedPlanStatus.pending,
        )
    ).first()
    if not plan:
        raise HTTPException(404, "No pending next plan")
    return plan


@router.delete("/{account_id}/next-plan")
def cancel_next_plan(account_id: int, session: Session = Depends(get_session), operator: str = Depends(require_auth)):
    """Cancel the pending next plan for this account."""
    if not session.get(Account, account_id):
        raise HTTPException(404, "Account not found")
    plan = session.exec(
        select(QueuedPlan).where(
            QueuedPlan.account_id == account_id,
            QueuedPlan.status == QueuedPlanStatus.pending,
        )
    ).first()
    if not plan:
        raise HTTPException(404, "No pending next plan to cancel")
    try:
        plan.status = QueuedPlanStatus.cancelled
        session.add(plan)
        session.add(
            AccountEvent(
                account_id=account_id,
                action="next_plan_cancelled",
                detail=f"Cancelled: {plan.data_limit_gb} GB / {plan.duration_days} days",
                created_by=operator,
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    return {"ok": True, "cancelled_plan_id": plan.id}


@router.post("/{account_id}/delete")
async def delete_account(account_id: int, session: Session = Depends(get_session), operator: str = Depends(require_auth)):
    """Permanently removes this Marzban user and marks the local account
    deleted (soft delete — see models.py's Account.deleted_at; every
    ledger/history row about it stays intact and reachable). Irreversible
    — there is no undo endpoint. The frontend confirms before calling
    this, same as every other destructive action on this page (see
    ShopPage.tsx's window.confirm() calls for the established pattern);
    nothing here re-confirms server-side.

    This is the operator's own version of what
    delegate_service.delete_delegate_account does for a delegate acting on
    their own scoped accounts — same close_out_payg_usage_before_delete /
    cancel_pending_queued_plan safety nets, no ownership/credit-limit
    checks because the operator already has full access."""
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    if account.deleted_at is not None:
        raise HTTPException(400, "This account is already deleted")

    final_charge = close_out_payg_usage_before_delete(
        session, account, source=LedgerSource.web, note="Final payg usage before delete", created_by=operator,
    )

    try:
        await marzban_client.delete_user(account.marzban_username)
    except ValueError as exc:
        raise HTTPException(400, f"Marzban rejected this: {exc}")
    except (MarzbanUnavailable, MarzbanAuthError) as exc:
        raise HTTPException(502, str(exc))

    if final_charge is not None:
        session.add(final_charge)
    account.deleted_at = utcnow()
    session.add(account)
    cancel_pending_queued_plan(session, account.id)
    session.add(AccountEvent(account_id=account.id, action="delete", detail="Deleted via dashboard", created_by=operator))
    session.commit()
    return {"ok": True}
