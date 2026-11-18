# 2026-11-18e — راند F: «کدوم اکانتم؟» + میانگینِ مصرف + toggle تمدیدِ خودکار

> **وضعیت: با GO مالک اجرا می‌شود.** سه قابلیتِ جدید، صفر تغییرِ قیمت/کسر.

## آنچه سوار می‌شود

| سرویس | تغییر | migration/DDL |
|---|---|---|
| `backend` | سشن: `renewable_label`؛ intentِ خریدِ ساده: `target_label` (فقط اسم؛ هیچ پولی جابه‌جا نمی‌شود)؛ ردیف‌ها: `auto_renew_enabled`؛ endpoint جدید `POST /accounts/{id}/auto-renew-toggle` (ownership هم‌سطح rename، AccountEvent audit) | **هیچ** |
| `shopbot` | صفحهٔ حجمِ فعلی‌ها: «این خرید به سرویس «X» اضافه می‌شود»؛ کارت‌ها: «مصرف روزانه: حدود X گیگ + با این روند تا N روز» (فقط وقتی ≥۰٫۰۵گیگ/روز)؛ «⋯ بیشتر»: دکمهٔ toggle با وضعیت («روشن — خاموشش کن» / «خاموش — روشنش کن»)؛ callback جدید `autotoggle:` | **هیچ** |
| `bot` / `delegate_bot` / `receipts_bot` / `frontend` | دست‌نخورده | — |

**کامیت:** `5de4b92` — قرمز-اول؛ ۱۶ سوییت ربات + ۱۶ بک‌اند سبز.

## سازگاری

- ربات قدیم + بک‌اند جدید: فیلدهای جدید نادیده؛ POST-username قدیمی ۴۰۹ می‌ماند؛ toggle فقط با ربات جدید.
- ربات جدید + بک‌اند قدیم: session/intent/rows فیلدها غایب → ربات به EXTEND_NOTE عمومی و «بدون toggle» تنزل می‌کند (کامیت‌های قبل هم همین الگو).

## مراحل + اسموک

بکاپ → ff → `--build backend` → `--build shopbot`؛ اسموک:

```bash
docker exec marzban-admin-panel-backend-1 python - <<'PY'
from app.schemas import ShopBotSession, ShopBotAccountRow, ShopBotAutoRenewToggleRequest
assert ShopBotSession.model_fields["renewable_label"] is not None
assert ShopBotAccountRow.model_fields["auto_renew_enabled"] is not None
import app.routers.shop
print("backend image OK")
PY
docker exec marzban-admin-panel-shopbot-1 python - <<'PY'
import texts, handlers.shop as shop
assert callable(texts.extend_target_note) and texts.AUTORENEW_STATE_ON
assert callable(shop.on_auto_toggle)
print("shopbot image OK")
PY
```

## نتیجهٔ اجرا (۲۰۲۶-۱۱-۱۸ ~16:56)

- پوش `f3cde02..abb8121`؛ سرور `--ff-only` به `abb8121`؛ درخت تمیز.
- بکاپ `pre-round-f-20261118-165559.db`.
- rebuild backend → اسموک سبز (سه فیلد/مدل جدید در تصویر)؛ rebuild shopbot → اسموک سبز (extend_target_note/AUTORENEW_STATE/on_auto_toggle)؛ لاگ پاک؛ پولینگ زنده.
- Rollback: `git checkout d03c59d -- backend/ shopbot/ && docker compose up -d --build backend shopbot`.
