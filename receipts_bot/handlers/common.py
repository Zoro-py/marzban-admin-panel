"""Single-chat gate + formatters — the receipts bot moves real money, so it
is single-operator by design (same rule as bot/handlers/common.py, kept
self-contained per the per-service trust-boundary convention)."""

from __future__ import annotations

import functools
import os
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))


def admin_only(handler):
    """Only the operator's chat id gets through — anyone else (or a group) is
    silently ignored."""

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
    from datetime import datetime, timezone

    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).strftime("%Y-%m-%d")


def display_name(user: Any) -> str:
    return " ".join(filter(None, [getattr(user, "first_name", None), getattr(user, "last_name", None)])) or ""
