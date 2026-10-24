"""Receipt approvals in the dedicated payments bot.

The approve/reject buttons under every relayed receipt carry callback data
`topup:ok:{id}` / `topup:no:{id}` (attached by the backend's
routers/shop.py::_alert_operator_to_topup). Since 2026-10-24 the receipts go
out via THIS bot's token (backend RECEIPTS_BOT_TOKEN), so the taps land here
— the operator bot's own topup handlers keep working for any older messages
still sitting in the operator chat.

The approval rules are copied verbatim in spirit from bot/handlers/topup.py:
approving is the single most money-sensitive action here — the backend
refuses anything not still pending (single-use), and this file must never
work around that guard.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from api_client import backend
from handlers.common import ADMIN_CHAT_ID, admin_only, format_toman


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_chat.id != ADMIN_CHAT_ID:
        return
    await update.message.reply_text(
        "💳 این بات فقط صندوقِ فیش‌های واریزی است.\n\n"
        "هر رسیدی که مشتری‌ها در بات فروش بفرستند، همین‌جا با دکمه‌های تأیید/رد می‌آید.\n"
        "/topups — لیست رسیدهای در انتظار\n"
        "/approve_<id> و /reject_<id> — تصمیم با کد\n\n"
        "تأیید از خود پنل هم می‌شود (فروشگاه → پرداخت‌ها)."
    )


@admin_only
async def topups_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists receipts still waiting — the fallback when the original
    notification was missed or its buttons no longer work."""
    try:
        pending = await backend.get("/api/shop/topups", params={"status": "pending"})
    except Exception as exc:  # noqa: BLE001 — the operator needs the reason
        await update.message.reply_text(f"خواندن رسیدهای در انتظار ممکن نشد: {exc}")
        return

    if not pending:
        await update.message.reply_text("رسیدی در انتظار تأیید نیست.")
        return

    lines = [f"{len(pending)} رسید در انتظار:", ""]
    for topup in pending[:20]:
        who = topup.get("display_name") or topup.get("telegram_id") or "نامشخص"
        code = f" [{topup['reference_code']}]" if topup.get("reference_code") else ""
        kind = f" · برای سفارش #{topup['order_id']}" if topup.get("order_id") else " · فقط شارژ کیف پول"
        lines.append(
            f"#{topup['id']}{code} — {who} — {format_toman(topup['claimed_amount'])}{kind}\n"
            f"   /approve_{topup['id']}   /reject_{topup['id']}"
        )
    if len(pending) > 20:
        lines.append(f"… و {len(pending) - 20} مورد دیگر (پنل → فروشگاه → پرداخت‌ها)")
    await update.message.reply_text("\n".join(lines))


async def _decide(topup_id: int, approve: bool) -> str:
    """Never raises — an exception here means the operator sees nothing and
    cannot tell whether the money moved."""
    action = "approve" if approve else "reject"
    try:
        result = await backend.post(f"/api/shop/topups/{topup_id}/{action}", json={})
    except ValueError as exc:
        return f"⚠️ #{topup_id}: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"❌ #{topup_id}: ارتباط با بک‌اند برقرار نشد — {exc}"

    if approve:
        amount = result.get("approved_amount") or result.get("claimed_amount") or 0
        if result.get("order_id"):
            return (f"✅ #{topup_id} تأیید شد — {format_toman(amount)} شارژ شد. "
                    f"برای سفارش #{result['order_id']} بود: اگر مبلغ کافی بود تحویل داده شد، "
                    "وگرنه به مشتری پیام رفته که چه مقدار کم است.")
        return f"✅ #{topup_id} تأیید شد — {format_toman(amount)} به کیف پولش اضافه شد."
    return f"❌ #{topup_id} رد شد. به مشتری پیام رفته."


@admin_only
async def topup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    try:
        _, action, raw_id = query.data.split(":", 2)
        topup_id = int(raw_id)
    except (ValueError, AttributeError):
        await query.edit_message_caption(caption="این دکمه خراب است — از /topups استفاده کن.")
        return
    if action not in ("ok", "no"):
        await query.message.reply_text(f"دکمهٔ ناشناس ({action}) — از /topups استفاده کن.")
        return

    outcome = await _decide(topup_id, approve=(action == "ok"))

    # The alert is usually a PHOTO, so its text lives in the caption — and the
    # buttons drop either way so a settled request can't be re-tapped.
    original = (query.message.caption if query.message.caption is not None else query.message.text) or ""
    new_text = f"{original}\n\n{outcome}"
    try:
        if query.message.caption is not None:
            await query.edit_message_caption(caption=new_text, reply_markup=None)
        else:
            await query.edit_message_text(text=new_text, reply_markup=None)
    except Exception:
        await query.message.reply_text(outcome)


@admin_only
async def approve_by_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    topup_id = _id_from_command(update.message.text, "approve")
    if topup_id is None:
        await update.message.reply_text("شکل درست: /approve_<id> — لیست: /topups")
        return
    await update.message.reply_text(await _decide(topup_id, approve=True))


@admin_only
async def reject_by_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    topup_id = _id_from_command(update.message.text, "reject")
    if topup_id is None:
        await update.message.reply_text("شکل درست: /reject_<id> — لیست: /topups")
        return
    await update.message.reply_text(await _decide(topup_id, approve=False))


def _id_from_command(text: str | None, verb: str) -> int | None:
    """Parses "/approve_12" (and "/approve_12@Bot" — Telegram appends the
    bot's username in groups)."""
    if not text:
        return None
    head = text.strip().split()[0].split("@")[0]
    prefix = f"/{verb}_"
    if not head.startswith(prefix):
        return None
    try:
        return int(head[len(prefix):])
    except ValueError:
        return None
