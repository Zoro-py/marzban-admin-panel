"""Bulk ("family") account creation: naming plan, subscription-link resolution,
and Telegram delivery of one QR per created account.

Kept out of services.py on purpose — services.py is the money model, and
nothing in this file computes, moves, or reads a balance. The only thing the
bulk path shares with billing is that it creates Account rows the normal
billing code later sees; it never posts a ledger entry itself.

Blast radius (AGENTS.md §6): MEDIUM. It calls Marzban's create endpoint in a
loop, which is an external side effect that cannot be rolled back — see
create_bulk_accounts in routers/accounts.py for the per-item commit policy
that follows from that (§4.5).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urljoin

from app.config import settings
from app.notify import SEND_SPACING_SECONDS, notify_admin, notify_admin_photo
from app.qr import subscription_qr_png

logger = logging.getLogger(__name__)

# Marzban's own username limit, mirrored by AccountCreateRequest's max_length.
# The numeric suffix counts against it, so a base name is capped lower.
MARZBAN_USERNAME_MAX_LEN = 32
# Upper bound on one batch. Not arbitrary: each item is a Marzban create plus a
# Telegram photo, so this is also the ceiling on how long the operator's chat is
# tied up and on how much damage a mistyped count can do in one click. Raising
# it means re-checking both of those, not just this number.
MAX_BULK_COUNT = 50
# Highest numeric suffix this will generate. Keeps a corrupt or hostile
# start_index from producing usernames that blow the length limit, and keeps
# the "continue from the last one" scan bounded.
MAX_NAME_INDEX = 99_999


@dataclass(frozen=True)
class PlannedName:
    index: int
    username: str
    already_exists: bool


@dataclass(frozen=True)
class BulkNamePlan:
    base_name: str
    start_index: int
    names: list[PlannedName]

    @property
    def free(self) -> list[PlannedName]:
        return [n for n in self.names if not n.already_exists]

    @property
    def taken(self) -> list[PlannedName]:
        return [n for n in self.names if n.already_exists]


@dataclass(frozen=True)
class DeliveryItem:
    username: str
    subscription_url: Optional[str]
    caption: str


def resolve_subscription_url(raw: Optional[str]) -> Optional[str]:
    """Turns Marzban's `subscription_url` into something a customer can open.

    Marzban returns an absolute URL when its own XRAY_SUBSCRIPTION_URL_PREFIX
    is configured, and a bare "/sub/<token>" path otherwise. An absolute value
    is passed through untouched — rewriting a host Marzban deliberately chose
    would silently break exactly the setups that bothered to configure one.
    """
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    base = (settings.marzban_subscription_base_url or settings.marzban_base_url or "").strip()
    if not base:
        # Nothing to resolve against. Returning the relative path would look
        # like a working link in the UI and fail only once a customer tried it.
        return None
    # urljoin drops the base's last path segment unless the base ends in "/",
    # so a panel served from "https://host/panel" would otherwise turn
    # "/sub/<token>" into "https://host/sub/<token>". Normalise rather than
    # hope every deployment happens to use a root-path base URL.
    if not base.endswith("/"):
        base += "/"
    return urljoin(base, raw.lstrip("/"))


def validate_base_name(base_name: str, *, highest_index: int) -> None:
    """Raises ValueError if the longest username this batch would generate
    doesn't fit Marzban's limit. Checked against the HIGHEST index in the
    batch, not the first — a batch starting at 9 and running to 11 gains a
    digit partway through, and failing on item 3 after creating 1 and 2 is
    exactly the half-done state this check exists to prevent."""
    longest = f"{base_name}{highest_index}"
    if len(longest) > MARZBAN_USERNAME_MAX_LEN:
        raise ValueError(
            f"Base name '{base_name}' is too long: this batch would need "
            f"'{longest}' ({len(longest)} characters), over Marzban's "
            f"{MARZBAN_USERNAME_MAX_LEN}-character limit. Shorten the base name "
            f"or create fewer accounts."
        )


def next_free_index(base_name: str, taken_lower: set[str]) -> int:
    """One past the highest numeric suffix already in use for this base name.

    Deliberately "highest + 1" and not "lowest unused": the operator asked for
    a family's accounts to read as a contiguous, ever-growing series
    (khanevade1..khanevade5, then khanevade6 next month). Back-filling a hole
    left by a deleted account would hand a NEW customer the username an OLD
    one had, and any config a customer still has saved for that name would
    start resolving to someone else's account.

    Suffixes are read with int(), so a legacy 'khanevade007' counts as 7 and
    the next name generated is 'khanevade8' — the series continues, it just
    stops being zero-padded from that point on.
    """
    pattern = re.compile(r"^" + re.escape(base_name.lower()) + r"(\d{1,6})$")
    highest = 0
    for name in taken_lower:
        match = pattern.match(name)
        if not match:
            continue
        index = int(match.group(1))
        # Suffixes ABOVE the cap are not part of a series this code manages —
        # they can only have come from somewhere else. Counting one would set
        # the next index above MAX_NAME_INDEX, and then EVERY future batch for
        # that base name fails with "would reach index N, above the limit",
        # permanently, with no way out but renaming the family. Ignoring it
        # means the series continues from the highest index we could have
        # produced ourselves, and any real collision is reported per-name.
        if index > MAX_NAME_INDEX:
            logger.warning(
                "Ignoring %r when continuing the %r series: its suffix is above the %d limit",
                name, base_name, MAX_NAME_INDEX,
            )
            continue
        highest = max(highest, index)
    return highest + 1


def plan_bulk_usernames(
    base_name: str,
    count: int,
    taken: Iterable[str],
    start_index: Optional[int] = None,
) -> BulkNamePlan:
    """Works out exactly which usernames a batch will create, before creating any.

    `taken` must be every username already in use — local rows AND Marzban's
    own users. Local-only would happily generate a name Marzban already has,
    and the operator would find out as a mid-batch failure.

    When `start_index` is given, the numbers are exactly start..start+count-1
    and any that are already taken are reported as taken rather than silently
    shifting the rest along. An operator who names a starting number is asking
    for specific usernames; quietly producing different ones (and a different
    final count) would be the surprise, not the collision report.
    """
    if count < 1:
        raise ValueError("count must be at least 1")
    if count > MAX_BULK_COUNT:
        raise ValueError(f"count must be at most {MAX_BULK_COUNT}")

    # Case-insensitive: whether two usernames differing only in case can
    # coexist depends on the collation of Marzban's own database, so treating
    # 'Khanevade1' as free when 'khanevade1' exists would create an account
    # that works on one panel and fails on another.
    taken_lower = {t.strip().lower() for t in taken if t and t.strip()}

    start = start_index if start_index is not None else next_free_index(base_name, taken_lower)
    if start < 1:
        raise ValueError("start_index must be at least 1")
    highest = start + count - 1
    if highest > MAX_NAME_INDEX:
        raise ValueError(f"This batch would reach index {highest}, above the {MAX_NAME_INDEX} limit")

    validate_base_name(base_name, highest_index=highest)

    names = [
        PlannedName(
            index=i,
            username=f"{base_name}{i}",
            already_exists=f"{base_name}{i}".lower() in taken_lower,
        )
        for i in range(start, highest + 1)
    ]
    return BulkNamePlan(base_name=base_name, start_index=start, names=names)


def format_plan_line(data_limit_gb: Optional[float], expire_days: Optional[int]) -> str:
    """Human-readable plan summary for the customer-facing caption.

    Persian, unlike the rest of this codebase, because this specific string is
    not read by an operator — it is forwarded verbatim to the customer, who
    reads Persian. Everything the operator sees (API fields, logs, the
    dashboard) stays English.

    "نامحدود"/"بدون انقضا" and not "0"/"-": Marzban's None means genuinely
    unlimited, and a customer reading "0 گیگ" would reasonably think their
    account was broken.
    """
    volume = f"{data_limit_gb:g} گیگابایت" if data_limit_gb else "حجم نامحدود"
    duration = f"{expire_days} روزه" if expire_days else "بدون انقضا"
    return f"{volume} · {duration}"


def build_caption(username: str, subscription_url: str, plan_line: str) -> str:
    """The message that goes out with each QR. Written to be forwarded to the
    customer as-is, so it carries nothing internal — no ids, no prices, no
    balances, no batch bookkeeping.

    The URL sits alone on the last line deliberately: a Latin URL inline with
    Persian text gets reordered by bidirectional rendering, and a customer
    copying it by hand can end up with a mangled link.
    """
    return f"🔑 {username}\n📦 {plan_line}\n\n{subscription_url}"


async def deliver_bulk_qr_messages(items: list[DeliveryItem], *, batch_label: str) -> None:
    """Sends one QR photo per account, then a summary.

    Runs as a background task, after the HTTP response has already gone out —
    so this is the ONLY channel that can tell the operator a send failed.
    That is why every failure is collected and reported in the summary rather
    than being allowed to abort the loop: the accounts themselves already
    exist in Marzban and are not undone by a failed notification, so the
    useful outcome is a list of which links still need fetching by hand.
    """
    if not settings.bot_token or not settings.bot_admin_chat_id:
        logger.warning(
            "Bulk batch %s: BOT_TOKEN/BOT_ADMIN_CHAT_ID unset, skipping %d QR message(s)",
            batch_label, len(items),
        )
        return

    failures: list[str] = []
    sent = 0
    for position, item in enumerate(items):
        if not item.subscription_url:
            failures.append(f"{item.username}: Marzban returned no subscription link")
            continue
        try:
            png = subscription_qr_png(item.subscription_url)
            await notify_admin_photo(png, item.caption, filename=f"{item.username}.png")
            sent += 1
        except Exception as exc:  # noqa: BLE001 — see docstring: never abort the batch
            logger.exception("Bulk batch %s: failed to send QR for %s", batch_label, item.username)
            failures.append(f"{item.username}: {exc}")
        if position < len(items) - 1:
            await asyncio.sleep(SEND_SPACING_SECONDS)

    summary = f"✅ {batch_label}: {sent} QR message(s) sent."
    if failures:
        # Capped: a batch where every send failed would otherwise build a
        # message Telegram rejects for length, losing the report entirely.
        shown = failures[:10]
        summary += f"\n⚠️ {len(failures)} failed:\n" + "\n".join(f"• {f}" for f in shown)
        if len(failures) > len(shown):
            summary += f"\n• …and {len(failures) - len(shown)} more (see server logs)"
    try:
        await notify_admin(summary)
    except Exception:
        logger.exception("Bulk batch %s: could not send the summary message", batch_label)
