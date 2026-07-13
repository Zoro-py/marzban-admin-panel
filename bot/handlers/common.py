import functools
import os
from datetime import datetime, timezone
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from api_client import backend

ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])


def admin_only(handler):
    """Every command is gated to one chat id — this bot manages real money and a
    live VPN panel, so it's single-operator by design, not multi-user."""

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat is None or update.effective_chat.id != ADMIN_CHAT_ID:
            return
        return await handler(update, context)

    return wrapper


def format_toman(amount: float) -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}{abs(round(amount)):,} T"


def format_gb(bytes_value: int | None) -> str:
    if bytes_value is None:
        return "∞"
    return f"{bytes_value / (1024 ** 3):.2f} GB"


def format_expire(unix_seconds: int | None) -> str:
    if unix_seconds is None:
        return "never"
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).strftime("%Y-%m-%d")


class AmbiguousMatch(Exception):
    def __init__(self, matches: list[dict]):
        self.matches = matches


async def resolve_customer(query: str) -> dict | None:
    """Looks up a customer by numeric id or case-insensitive name substring.
    Raises AmbiguousMatch if more than one name matches."""
    if query.isdigit():
        customers: list[dict] = await backend.get("/api/customers")
        for c in customers:
            if c["id"] == int(query):
                return c
        return None

    customers = await backend.get("/api/customers")
    matches = [c for c in customers if query.lower() in c["name"].lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AmbiguousMatch(matches)
    return None


async def resolve_account(username: str) -> dict | None:
    accounts: list[dict] = await backend.get("/api/accounts")
    for a in accounts:
        if a["marzban_username"] == username:
            return a
    return None


def reply_kwargs() -> dict[str, Any]:
    return {"parse_mode": "Markdown"}
