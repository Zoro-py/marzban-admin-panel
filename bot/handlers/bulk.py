"""/bulk — create a family batch of accounts from one base name.

Two steps on purpose. The command itself only PREVIEWS: it asks the backend
which usernames are free and shows them, and nothing is created until the
operator taps the confirm button. Creating N accounts in Marzban cannot be
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
    "Usage: /bulk <name> <count> [30gb] [30d] [from=7]\n\n"
    "Examples:\n"
    "  /bulk khanevade 5\n"
    "  /bulk khanevade 5 30gb 30d\n"
    "  /bulk khanevade 3 50gb 60d from=8\n\n"
    "Creates <name>1, <name>2, … continuing after the highest number that "
    "already exists, unless you give from=N. Nothing is charged."
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
        else:
            # Never silently ignored: a typo like "30g" would otherwise create
            # unlimited accounts while the operator believed they were 30GB.
            raise ValueError(
                f"Didn't understand '{token}'. Use 30gb for volume, 30d for days, "
                f"from=8 to start at a specific number."
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

    token = uuid.uuid4().hex[:12]
    context.user_data[_PENDING_KEY] = {"token": token, "body": body}

    lines = [
        f"About to create {len(free)} account(s) — {_plan_summary(body)}",
        "",
        *[f"• {n['marzban_username']}" for n in free],
    ]
    if taken:
        lines += ["", f"Skipping {len(taken)} that already exist:"]
        lines += [f"• {n['marzban_username']}" for n in taken]
    lines += ["", "Nothing is charged. Confirm to create them in Marzban."]

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Create {len(free)}", callback_data=f"bulk:go:{token}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"bulk:no:{token}"),
    ]])
    # No parse_mode: usernames legitimately contain underscores, which Markdown
    # would either mangle or reject outright as unbalanced entities.
    await update.message.reply_text("\n".join(lines), reply_markup=keyboard)


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

    # Consumed BEFORE the slow call, not after: a batch takes minutes, and a
    # second tap during that window would otherwise start an identical batch.
    context.user_data.pop(_PENDING_KEY, None)
    body = pending["body"]
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
