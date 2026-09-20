"""/bulk — create a family batch of accounts from one base name.

Three steps on purpose. The command itself only PREVIEWS: it asks the backend
which usernames are free and shows them, and nothing is created until the
operator taps a confirm button — which also settles WHO the batch belongs to
(a new customer named after the batch, an existing cust=/group= given in the
command, or explicitly nobody). Unassigned accounts can never be billed (settle
refuses them, the monthly job skips them), so the new-family-customer route is
the safe default and comes first. Creating N accounts in Marzban cannot be
undone, and a mistyped count in a chat window is far too easy — a one-shot
command would make "/bulk khanevade 50" (meant as 5) an irreversible mistake
with no moment to catch it.

The QR/link messages themselves are sent by the BACKEND (app/bulk_accounts.py),
not from here, so they still arrive even when this bot process is down.
"""

from __future__ import annotations

import re
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from api_client import backend
from handlers.common import admin_only

# Mirrors backend/app/bulk_accounts.py's MAX_BULK_COUNT. Duplicated because
# the bot is a separate process with no import path into the backend; if that
# limit changes, this line has to change with it.
MAX_BULK_COUNT = 50

USAGE = (
    "Usage: /bulk <name> <count> [30gb] [30d] [from=7] [cust=<id|name>] [group=<id>]\n\n"
    "Examples:\n"
    "  /bulk khanevade 5\n"
    "  /bulk khanevade 5 30gb 30d\n"
    "  /bulk khanevade 3 50gb 60d from=8 cust=Ali\n"
    "  /bulk khanevade 3 cust=12 group=2\n\n"
    "Creates <name>1, <name>2, … continuing after the highest number that "
    "already exists, unless you give from=N. Nothing is charged.\n\n"
    "Assignment: without cust=/group= the confirm step offers to create a new "
    "customer named after the batch (the safe default — unassigned accounts "
    "can't be billed), or create them with no owner."
)

# Telegram caps callback_data at 64 bytes, and a 28-character base name plus
# the plan fields does not reliably fit. So the parsed request is held in
# user_data and only a short token travels in the button.
_PENDING_KEY = "pending_bulk"

# A batch is one Marzban create per account; the backend also pages the whole
# user list once up front. Five minutes is generous enough for the 50-account
# ceiling on a slow panel without hanging forever on a genuinely dead backend.
CREATE_TIMEOUT_SECONDS = 300

_GB_RE = re.compile(r"^(\d+(?:\.\d+)?)gb$", re.IGNORECASE)
_DAYS_RE = re.compile(r"^(\d+)d$", re.IGNORECASE)
_FROM_RE = re.compile(r"^(?:from|start)=(\d+)$", re.IGNORECASE)
_CUST_RE = re.compile(r"^cust=(.+)$", re.IGNORECASE)
_GROUP_RE = re.compile(r"^group=(\d+)$", re.IGNORECASE)


def parse_bulk_args(args: list[str]) -> dict:
    """Suffix-tagged rather than positional ("30gb", "30d", "from=8").

    Positional arguments were the obvious first design and are the wrong one
    here: `/bulk khanevade 5 30 30` gives the operator no way to see, in their
    own sent message, whether they typed GB-then-days or days-then-GB — and
    the two produce very different accounts. A tagged token is self-checking
    when you read the message back.

    Raises ValueError with a message meant to be shown to the operator.
    """
    if len(args) < 2:
        raise ValueError(USAGE)

    base_name = args[0]
    if not re.fullmatch(r"[a-zA-Z0-9_]{2,28}", base_name):
        raise ValueError(
            f"'{base_name}' can't be used as a base name — letters, numbers and "
            f"underscore only, 2 to 28 characters."
        )

    try:
        count = int(args[1])
    except ValueError:
        raise ValueError(f"'{args[1]}' is not a number. {USAGE}")
    if not 1 <= count <= MAX_BULK_COUNT:
        # Checked here as well as in the schema: the backend's 422 reaches the
        # operator as a raw validation blob, which reads like a broken bot.
        raise ValueError(f"Count must be between 1 and {MAX_BULK_COUNT} — got {count}.")

    body: dict = {"base_name": base_name, "count": count}

    for token in args[2:]:
        if (m := _GB_RE.match(token)):
            body["data_limit_gb"] = float(m.group(1))
        elif (m := _DAYS_RE.match(token)):
            body["expire_days"] = int(m.group(1))
        elif (m := _FROM_RE.match(token)):
            body["start_index"] = int(m.group(1))
        elif (m := _CUST_RE.match(token)):
            # Held aside, not sent to the backend: the schema takes a
            # customer_id, which the command resolves by id or exact name
            # before anything is created.
            body["_assign_customer"] = m.group(1).strip()
        elif (m := _GROUP_RE.match(token)):
            body["_assign_group"] = int(m.group(1))
        else:
            # Never silently ignored: a typo like "30g" would otherwise create
            # unlimited accounts while the operator believed they were 30GB.
            raise ValueError(
                f"Didn't understand '{token}'. Use 30gb for volume, 30d for days, "
                f"from=8 to start at a specific number, cust=<id|name> or group=<id> "
                f"to assign the batch."
            )
    return body


def _plan_summary(body: dict) -> str:
    volume = f"{body['data_limit_gb']:g} GB" if body.get("data_limit_gb") else "unlimited"
    days = f"{body['expire_days']} days" if body.get("expire_days") else "never expires"
    return f"{volume} · {days}"


@admin_only
async def bulk_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        body = parse_bulk_args(context.args or [])
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    assign_customer = body.pop("_assign_customer", None)
    assign_group = body.pop("_assign_group", None)
    if assign_customer is not None and assign_group is not None:
        await update.message.reply_text(
            "Assign the batch to a customer OR a group, not both — run /bulk again with one of cust=/group=.")
        return

    try:
        preview = await backend.post("/api/accounts/bulk/preview", json=body)
    except ValueError as exc:
        await update.message.reply_text(f"Couldn't plan this batch: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 — the operator needs to see this, not a silent no-op
        await update.message.reply_text(f"Couldn't reach the backend: {exc}")
        return

    free = [n for n in preview["names"] if not n["already_exists"]]
    taken = [n for n in preview["names"] if n["already_exists"]]

    if not free:
        await update.message.reply_text(
            "Every name in that range already exists:\n"
            + "\n".join(f"• {n['marzban_username']}" for n in taken)
            + "\n\nUse a different from= number, or a different base name."
        )
        return

    # Resolve the explicit assignment BEFORE anything exists to own — a batch
    # created for a mistyped customer name must not go out unassigned just
    # because nobody checked the name.
    create_body = dict(body)
    assignment_label = None
    if assign_customer is not None or assign_group is not None:
        try:
            if assign_customer is not None:
                customers = await backend.get("/api/customers")
                match = next(
                    (c for c in customers if str(c["id"]) == assign_customer
                     or c["name"].strip().lower() == assign_customer.lower()),
                    None,
                )
                if match is None:
                    await update.message.reply_text(
                        f"No customer '{assign_customer}' — rerun with cust=<id>, or drop "
                        f"cust= and pick «New customer» on the confirm step.")
                    return
                create_body["customer_id"] = match["id"]
                assignment_label = f"customer {match['name']} (id {match['id']})"
            else:
                groups = await backend.get("/api/groups")
                match = next((g for g in groups if g["id"] == assign_group), None)
                if match is None:
                    await update.message.reply_text(
                        f"No group with id {assign_group} — check /api/groups or drop group=.")
                    return
                create_body["group_id"] = match["id"]
                assignment_label = f"group {match['name']} (id {match['id']})"
        except Exception as exc:  # noqa: BLE001
            await update.message.reply_text(f"Couldn't verify the assignment: {exc}")
            return

    token = uuid.uuid4().hex[:12]
    context.user_data[_PENDING_KEY] = {"token": token, "body": create_body}

    lines = [
        f"About to create {len(free)} account(s) — {_plan_summary(body)}",
        f"Assigned to: {assignment_label}" if assignment_label else "Assignment: none yet",
        "",
        *[f"• {n['marzban_username']}" for n in free],
    ]
    if taken:
        lines += ["", f"Skipping {len(taken)} that already exist:"]
        lines += [f"• {n['marzban_username']}" for n in taken]
    lines += ["", "Nothing is charged. Confirm to create them in Marzban."]

    if assignment_label:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Create {len(free)}", callback_data=f"bulk:go:{token}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"bulk:no:{token}"),
        ]])
    else:
        # The assignment step, with the safe default first: an unassigned batch
        # can never be billed (settle refuses it, the monthly job skips it), so
        # the new-family-customer route is the one the operator most likely wants.
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"👨‍👩‍👧 Create + one family customer «{body['base_name']}»",
                                  callback_data=f"bulk:asnew:{token}")],
            [InlineKeyboardButton(f"👤 Create {len(free)} without owner", callback_data=f"bulk:go:{token}"),
             InlineKeyboardButton("❌ Cancel", callback_data=f"bulk:no:{token}")],
        ])
    # No parse_mode: usernames legitimately contain underscores, which Markdown
    # would either mangle or reject outright as unbalanced entities.
    await update.message.reply_text("\n".join(lines), reply_markup=keyboard)


async def _create_batch(query, body: dict) -> None:
    """The slow shared tail of both confirm routes (assigned or not): POST the
    batch and render whatever actually happened — created, failed, untracked."""
    await query.edit_message_text("Creating… this can take a minute. The QR codes will arrive as they're made.")

    try:
        result = await backend.post("/api/accounts/bulk", json=body, timeout=CREATE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — see below: silence here is the dangerous outcome
        # Deliberately not swallowed. The request may well have created
        # accounts before whatever went wrong, so "nothing happened" is NOT a
        # safe assumption to leave the operator with.
        await query.edit_message_text(
            f"The batch request failed: {exc}\n\n"
            f"Some accounts may still have been created — check the Accounts page or /sync before retrying."
        )
        return

    lines = [f"{result['created']} created, {result['skipped']} skipped, {result['failed']} failed."]
    for warning in result.get("warnings", []):
        lines.append(f"⚠️ {warning}")
    if result.get("customer_name") and result["created"]:
        lines.append(f"👨‍👩‍👧 Owner: «{result['customer_name']}» (customer #{result['customer_id']}) — one payer for the whole batch.")
    if result.get("aborted_reason"):
        lines.append(f"⚠️ Stopped early: {result['aborted_reason']}")
    failed = [i for i in result["items"] if i["status"] == "failed"]
    if failed:
        lines.append("")
        lines += [f"❌ {i['marzban_username']}: {i['error']}" for i in failed[:10]]
        if len(failed) > 10:
            lines.append(f"…and {len(failed) - 10} more")
    untracked = [i for i in result["items"] if i["status"] == "created_untracked"]
    if untracked:
        lines.append("")
        lines.append(
            "⚠️ Created in Marzban but not tracked locally (they'll be picked up by the next sync): "
            + ", ".join(i["marzban_username"] for i in untracked)
        )
    if not result["notifications_queued"]:
        lines.append("")
        lines.append("No QR messages were queued — BOT_TOKEN/BOT_ADMIN_CHAT_ID isn't set on the backend.")

    await query.edit_message_text("\n".join(lines))


@admin_only
async def bulk_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    try:
        _, action, token = query.data.split(":", 2)
    except ValueError:
        await query.edit_message_text("This button is malformed — run /bulk again.")
        return

    pending = context.user_data.get(_PENDING_KEY)
    # The token check is what makes an OLD message's button harmless. Without
    # it, scrolling up and tapping confirm on a previous batch would create a
    # second batch the operator never asked for.
    if not pending or pending.get("token") != token:
        await query.edit_message_text(
            "This batch is no longer pending (the bot restarted, or a newer /bulk replaced it). "
            "Run /bulk again."
        )
        return

    if action == "no":
        context.user_data.pop(_PENDING_KEY, None)
        await query.edit_message_text("Cancelled — nothing was created.")
        return

    # Only the two confirm actions may create anything. An unknown/garbled
    # action used to fall through to «create the batch», so it is refused here,
    # BEFORE the pending batch is consumed (the operator can still confirm).
    if action not in ("go", "asnew"):
        await query.edit_message_text("This button isn't recognised — run /bulk again.")
        return

    # Consumed BEFORE the slow call, not after: a batch takes minutes, and a
    # second tap during that window would otherwise start an identical batch.
    context.user_data.pop(_PENDING_KEY, None)
    body = pending["body"]

    if action == "asnew":
        # The safe default from the confirm step. The backend owns the rule now
        # (POST /api/accounts/bulk with no owner attaches the batch to one new
        # or same-named «family» customer, created with the first account), so
        # the bot no longer duplicates it — one implementation for panel and
        # bot, and no window where the customer exists but the batch failed.
        body = {k: v for k, v in body.items() if k not in ("customer_id", "group_id", "unassigned")}
    elif action == "go" and body.get("customer_id") is None and body.get("group_id") is None:
        # The explicit «without owner» button: say so, because an owner-less
        # request is now the trigger for the family default.
        body = {k: v for k, v in body.items() if k not in ("customer_id", "group_id")}
        body["unassigned"] = True

    await _create_batch(query, body)
