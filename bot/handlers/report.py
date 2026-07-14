from telegram import Update
from telegram.ext import ContextTypes

from api_client import backend
from handlers.common import admin_only, format_toman


@admin_only
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    summary = await backend.get("/api/reports/summary")

    lines = [f"*Daily summary* — {summary['total_customers']} customers, {summary['total_accounts']} accounts", ""]

    lines.append(f"*Overdue customers ({len(summary['overdue_customers'])})*")
    if not summary["overdue_customers"]:
        lines.append("None owe anything.")
    for c in summary["overdue_customers"][:15]:
        lines.append(f"• {c['name']} — {format_toman(c['balance'])}")

    lines.append("")
    lines.append(f"*Exhausted, out of quota ({len(summary['exhausted_accounts'])})*")
    if not summary["exhausted_accounts"]:
        lines.append("None.")
    for a in summary["exhausted_accounts"][:15]:
        lines.append(f"• `{a['marzban_username']}` — {a['used_pct']}%")

    lines.append("")
    lines.append(f"*Near quota, ≥80% ({len(summary['near_quota_accounts'])})*")
    if not summary["near_quota_accounts"]:
        lines.append("None.")
    for a in summary["near_quota_accounts"][:15]:
        lines.append(f"• `{a['marzban_username']}` — {a['used_pct']}%")

    lines.append("")
    lines.append(f"*Already expired ({len(summary['expired_accounts'])})*")
    if not summary["expired_accounts"]:
        lines.append("None.")
    for a in summary["expired_accounts"][:15]:
        lines.append(f"• `{a['marzban_username']}` — {abs(a['days_left'])}d ago")

    lines.append("")
    lines.append(f"*Expiring within 3 days ({len(summary['near_expiry_accounts'])})*")
    if not summary["near_expiry_accounts"]:
        lines.append("None.")
    for a in summary["near_expiry_accounts"][:15]:
        lines.append(f"• `{a['marzban_username']}` — {a['days_left']}d left")

    lines.append("")
    lines.append(f"*No rate configured ({len(summary['no_rate_accounts'])})*")
    if not summary["no_rate_accounts"]:
        lines.append("None.")
    for a in summary["no_rate_accounts"][:15]:
        lines.append(f"• `{a['marzban_username']}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
