# 2026-11-18 — دیپلوی راند C (گیتِ دکمهٔ تمدیدِ کهنه) + پذیرشِ رسیدِ PDF

> **وضعیت: با GO مالک اجرا می‌شود — همین روز.** الگو و ترتیب، همان برگهٔ
> [`2026-11-17_shopbot_rounds_ab.md`](2026-11-17_shopbot_rounds_ab.md) است.

## آنچه سوار می‌شود

| سرویس | تغییر | migration/DDL |
|---|---|---|
| `backend` | M-2: دکمه‌های تمدیدِ هشدارها زمانِ ساخت را در callback حمل می‌کنند (`warning_renew_callback`)؛ PDF: relay اپراتور برای PDF `sendDocument` می‌زند (تشخیص از پسوندِ file_path یا مَجیکِ `%PDF-`) | **هیچ** |
| `shopbot` | M-2: گیتِ ۷روزه روی تپِ دکمه‌های تمدیدِ مهرزده (پاسِ آرام + پاک‌کردن مارک‌آپ؛ دکمه‌های بی-ts سازگار)؛ PDF: `handle_document` رسیدهای PDF را مثل عکس می‌پذیرد؛ ۷ متن، PDF را در فرم‌های قبول نام می‌برند | **هیچ** |
| `bot` / `delegate_bot` / `receipts_bot` / `frontend` | **دست‌نخورده** | — |

**کامیت‌ها:** `17165b1` (M-2 کد+تست) → `ae7ccb7` (سند) → `dd9d9b6` (PDF کد+تست) — سبز: ۱۵ سوییت ربات + ۱۳ سوییت بک‌اند + `py_compile`. هر دو قرمز-اولِ اندازه‌گیری‌شده دارند (رباتِ M-2: ۱۲ فیل شامل دو سکوتِ مطلق؛ رباتِ PDF: ۱۱ فیل؛ رلهٔ PDF: کرش روی sendPhoto هاردکد).

## سازگاری (مستند، تست‌شده)

- دکمه‌های بی-ts (چت‌های قدیمی، پنجرهٔ بک‌اندِ قدیم): کار می‌کنند — قانونِ compat.
- old-bot/new-backend: فرمتِ ۴تکه → پاسِ آرامِ «قدیمی شده» — تنزل امن.
- new-bot/old-backend: بی-ts → کار می‌کند؛ رلهٔ PDF فقط با بک‌اندِ جدید معنا دارد (بک‌اند اول دیپلوی می‌شود).
- receipts_bot: دکمه‌های تأیید/رد روی پیامِ document هم کار می‌کنند (CallbackQueryHandler مستقل از نوع پیام).

## مراحل (روی france-vpn) — همان الگوی 11-17

```bash
cd /opt/marzban-admin-panel
git fetch origin feat/shopbot-ux-rebuild && git merge --ff-only origin/feat/shopbot-ux-rebuild

# بکاپ sqlite (API رسمی، WAL-safe) — دستور در برگهٔ 11-17؛ نامِ امشب:
#   pre-shopbot-round-c-pdf-<ts>.db
docker compose up -d --build backend    # اول
docker compose up -d --build shopbot    # بعد
```

## اسموک تصویر

```bash
docker exec marzban-admin-panel-backend-1 python - <<'PY'
from app import notify, shop_service
ns = open(notify.__file__, encoding="utf-8").read()
assert "sendDocument" in ns and "%PDF-" in ns
assert callable(shop_service.warning_renew_callback)
print("backend image OK")
PY

docker exec marzban-admin-panel-shopbot-1 python - <<'PY'
import texts, handlers.shop as shop
assert shop._RENEW_BUTTON_MAX_AGE_S == 7 * 86400
assert "PDF" in texts.topup_instructions(100_000, "6037", None, 30)
src = open("handlers/shop.py", encoding="utf-8").read()
assert "application/pdf" in src
print("shopbot image OK")
PY
```

اسموکِ تلگرامیِ دستِ مالک (بخشِ مربوط در برگهٔ 11-06) + یک موردِ جدید: با اکانتِ دورریز یک رسیدِ PDF بفرستید — باید «✅ رسیدتان با موفقیت دریافت شد» بیاید و در receipts_bot به‌صورتِ document با دکمه‌ها ظاهر شود.

## Rollback

هیچ DDL در کار نیست:

```bash
cd /opt/marzban-admin-panel
git checkout 51c6d17 -- backend/ shopbot/
docker compose up -d --build backend shopbot
```

## نتیجهٔ اجرا (۲۰۲۶-۱۱-۱۷ ~22:09–22:11 به وقتِ سرور)

- **پوش:** `ae7ccb7..b1a7663 → origin`؛ مرجِ `--ff-only` سرور به `b1a7663` — درختِ tracked تمیز.
- **بکاپ:** `pre-shopbot-round-c-pdf-20261117-220954.db`، **۱۳۰٬۲۸۵٬۵۶۸ بایت** (API رسمی، WAL-safe).
- **backend:** rebuild → Up؛ اسموک سبز (`sendDocument`+`%PDF-` در تصویر، `warning_renew_callback` موجود، روترها import)؛ اسکنِ لاگ صفر خطا.
- **shopbot:** rebuild → Up؛ اسموک سبز (`_RENEW_BUTTON_MAX_AGE_S`=۷روز، PDF در پرامپت‌ها، `application/pdf` در تصویر، `RENEW_BUTTON_STALE` موجود)؛ `Application started` + getUpdates 200ها — پولینگِ زنده.
- **دست‌نخورده‌ها:** bot / delegate_bot / receipts_bot / frontend (Up ۳هفته).
- **باقی‌مانده برای مالک:** اسموکِ تلگرامی + موردِ جدیدِ PDF (با اکانتِ دورریز یک رسیدِ PDF بفرستید → «✅ رسیدتان با موفقیت دریافت شد» + در receipts_bot به‌صورت document با دکمه‌ها).
- **Rollback:** `git checkout 51c6d17 -- backend/ shopbot/ && docker compose up -d --build backend shopbot` (بکاپِ بالا موجود).
