# 2026-11-18c — سیاستِ نام‌گذاریِ جدید: نامِ کاربریِ اجباری + یونیکِ خودکار + nudge نام‌گذاری

> **وضعیت: با GO مالک اجرا می‌شود.** سه تصمیمِ مالک در یک راند: ① nudge نام‌گذاری (پیشنهادِ اعمال‌شده) ② حذفِ نام‌گذاریِ خودکار ③ تکراری → یونیکِ عددی.

## آنچه سوار می‌شود

| سرویس | تغییر | migration/DDL |
|---|---|---|
| `backend` | `_resolve_unique_username` (ali → ali-2…-50، برشِ ۳۰کاراکتری، خطای فارسی فقط وقتی هر ۵۰ گرفته)؛ endpointِ `orders/{id}/username` یونیک می‌کند و `was_taken` برمی‌گرداند؛ **provision دوباره resolve می‌کند** — فاصلهٔ سفارش→تأیید می‌تواند ساعت‌ها باشد و نام در این میان گرفته شود (هر-۵۰-گرفته در provision → refund، هرگز دوبار‌کسر)؛ `bot_purchase`/`orders` POST برای ربات‌های قدیمی ۴۰۹ می‌مانند | **هیچ** |
| `shopbot` | پرسشِ نامِ کاربری **اجباری** روی هر خریدِ سرویسِ نو (`intent.creates_new`) — بین سفارش و پرداخت؛ تمدیدِ درجا و `ren:` هرگز نمی‌پرسند؛ «بدون نام» بازنشسته (`AUTO_NAME_RETIRED`)؛ PAYG اجباری؛ صفحهٔ پرداختِ نامِ یونیک‌شده را اعلام می‌کند؛ دکمهٔ «نام کاربری دلخواه» از صفحهٔ حجم حذف؛ **hint ملایمِ نام‌گذاری** یک‌بار زیرِ کارت‌های مشتریِ چند-سرویسیِ بی‌نام | **هیچ** |
| `bot` / `delegate_bot` / `receipts_bot` / `frontend` | دست‌نخورده | — |

**کامیت:** `692e8d9` — قرمز-اول دوطرفه؛ `test_buy_flow` بازنویسی به اسپکِ جدید؛ ۱۵ سوییت ربات + ۱۵ بک‌اند سبز.

## رفتارِ جدید (خلاصه برای اسموکِ چشمی)

- نوآمد: «خرید» → حجم → **«نام کاربری سرویس را انتخاب کنید…»** (فقط انصراف+منو، بدون «بدون نام») → رسید/تأیید. اسمِ فارسی → قاعده؛ `ali` تکراری → بی‌سروصدا `ali-2` و روی صفحهٔ پرداخت: «نام «ali» قبلاً گرفته شده بود — نام کاربری سرویس: ali-2 شد.»
- مشتریِ فعلی از «خرید / تمدید» (تمدید درجا) و دکمهٔ تمدیدِ کارت‌ها: بدون پرسشِ نام.
- «سرویس جداگانه»: پرسشِ نام دارد (سرویسِ نو می‌سازد).
- PAYG: نام اجباری.
- «سرویس‌های من» مشتریِ ≥۲سرویسه با سرویسِ بی‌نام → یک‌بار: «💡 … نام بدهید: ⋯ بیشتر ← ✏️ نام سرویس».

## مراحل — همان الگو (بک‌اند اول، بعد shopbot)

```bash
cd /opt/marzban-admin-panel
git fetch origin feat/shopbot-ux-rebuild && git merge --ff-only origin/feat/shopbot-ux-rebuild
# بکاپ sqlite (API رسمی) با نامِ pre-naming-policy-<ts>.db
docker compose up -d --build backend && sleep 6
docker compose up -d --build shopbot && sleep 5
```

## اسموک تصویر

```bash
docker exec marzban-admin-panel-backend-1 python - <<'PY'
import asyncio
from app import shop_service
async def t():
    assert await shop_service._resolve_unique_username("zz-free-name") == "zz-free-name"
import app.routers.shop
print("backend image OK")
PY
docker exec marzban-admin-panel-shopbot-1 python - <<'PY'
import texts, handlers.shop as shop
assert texts.AUTO_NAME_RETIRED and texts.NAMING_HINT
assert callable(texts.username_taken_note)
assert shop._NAMING_HINT_DONE
src = open("handlers/shop.py", encoding="utf-8").read()
assert "_PENDING_INTENT" in src and "creates_new" in src
print("shopbot image OK")
PY
```

## سازگاری

- رباتِ قدیم + بک‌اندِ جدید: username در POST سفارش → هنوز ۴۰۹ِ فارسیِ «قبلاً گرفته شده» (رفتارِ قدیمیِ قابل‌اداره)؛ نامِ نداده → auto `shop{id}` (fallback بک‌اند سر جایش است، فقط رباتِ جدید دیگر از آن استفاده نمی‌کند).
- رباتِ جدید + بک‌اندِ قدیم: intent بدون `creates_new` → falsy → بدون ask → نام ندیده → auto — تنزلِ امن (پنجرهٔ بین دو rebuild).
- هیچ تغییرِ قیمت/کسر/برگشت؛ uniquify فقط رشتهٔ نام است.

## Rollback

```bash
cd /opt/marzban-admin-panel
git checkout b1a7663 -- backend/ shopbot/
docker compose up -d --build backend shopbot
```

## نتیجهٔ اجرا

- (پس از اجرا ثبت می‌شود.)
