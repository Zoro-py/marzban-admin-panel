# 2026-11-17 — دیپلوی راندهای A+B شاپ‌بات (فیکسِ کرشِ نام‌کاربری + یکدست‌سازیِ کپیِ بک‌اند)

> **وضعیت: با GO مالک اجرا شد — همین روز.** ترتیب و اسموکِ زیر همان برگهٔ
> [`2026-11-06_shopbot_ux.md`](2026-11-06_shopbot_ux.md) است (تست‌شده دو جهت)، با دلتای این دو راند.

## آنچه سوار می‌شود

| سرویس | تغییر | migration/DDL |
|---|---|---|
| `backend` | کپیِ پوش یکدست با واژگان T5/R2 (`shop_texts.py`: همین‌جا، کسرشی، همزه، بدهیِ تسویه‌نشده حذف)؛ برچسبِ دکمه per-receiver با `has_accounts` (همان هلپر `is_existing_customer`)؛ رله‌های pay-debt به ثابت‌های `PAY_DEBT_*`؛ دکمهٔ هشدار `WARNING_RENEW_BUTTON`؛ حذف `payment_request` مرده | **هیچ** — فقط رشته و یک SELECTِ فقط-خواندنی در مسیرِ اعلانِ تأیید |
| `shopbot` | `USERNAME_INVALID` (فیکسِ کرشِ زندهٔ مسیرِ نام) + مثالِ درستِ `ASK_PREFERRED_NAME` + `debt_receipt_done` با واژگانِ درست | **هیچ** |
| `bot` / `delegate_bot` / `receipts_bot` / `frontend` | **دست‌نخورده** | — |

**کامیت‌ها:** `b9f8a91` (راند A کد+تست) → `67e9965` (سند) → `efcc1ea` (راند B کد+تست) → `7d3ab6a` (سند) — همه روی `feat/shopbot-ux-rebuild`، سبز: ۱۳ سوییت ربات + ۱۰ سوییت بک‌اند + ۱۹ چک کپی + `py_compile`.

## ترتیب: اول backend، بعد shopbot (همان دلیلِ برگهٔ 11-06: رباتِ جدید/بک‌اندِ قدیم و برعکس هر دو تست شده)

## پیش‌نیازها

1. GO مالک ✓ (۲۰۲۶-۱۱-۱۷) → پوشِ شاخه به origin.
2. همهٔ سوییت‌ها سبز ✓ (آخرین اجرا: همین راند).

## مراحل (روی france-vpn)

```bash
cd /opt/marzban-admin-panel
git fetch origin feat/shopbot-ux-rebuild
git merge --ff-only origin/feat/shopbot-ux-rebuild   # 8dc4755 → 7d3ab6a

# بکاپِ sqlite با API رسمی (WAL-safe، برخلاف cp خام):
docker exec marzban-admin-panel-backend-1 python - <<'PY'
import sqlite3, time
dst = f"/app/data/manual_backups/pre-shopbot-rounds-ab-{time.strftime('%Y%m%d-%H%M%S')}.db"
src = sqlite3.connect("/app/data/vpn.db")
dst_c = sqlite3.connect(dst)
src.backup(dst_c)
dst_c.close()
print("backup ->", dst)
PY

docker compose up -d --build backend   # اول
docker compose up -d --build shopbot   # بعد
```

## اسموک (بدون اکانتِ تلگرامیِ دورریز — راستی‌آزماییِ تصویرِ در حال اجرا)

```bash
docker exec marzban-admin-panel-backend-1 python - <<'PY'
from app import shop_texts as st
assert st.MENU_BUY_RENEW_LABEL == "🛒 خرید / تمدید سرویس"
assert st.PAY_DEBT_NONE_DETAIL == "ماندهٔ پرداخت‌نشده‌ای ندارید."
src = open(st.__file__, encoding="utf-8").read()
assert "کسرشی" not in src and "همینجا" not in src and "بدهیِ صورت‌حسابی" not in src
import app.routers.shop  # کامپایلِ کاملِ روترِ دیپلوشده
print("backend copy OK")
PY

docker exec marzban-admin-panel-shopbot-1 python - <<'PY'
import texts
assert texts.USERNAME_INVALID  # تا دیروز AttributeError می‌داد
assert "fateme" in texts.ASK_PREFERRED_NAME and "fateme." not in texts.ASK_PREFERRED_NAME
done = texts.debt_receipt_done("ABCD", 30)
assert "صورت‌حساب" not in done and "ماندهٔ پرداخت‌نشده" in done
import handlers.shop
print("shopbot copy OK")
PY
```

اسموکِ ۷مرحله‌ایِ تلگرامی (با اکانتِ دورریز) سرِ جای خودش در برگهٔ 11-06 می‌ماند — دستِ مالک.

## Rollback

هیچ DDL و هیچ نوشدنیِ داده‌ای در کار نیست — برگشتِ کد و rebuild:

```bash
cd /opt/marzban-admin-panel
git checkout 8dc4755 -- backend/ shopbot/
docker compose up -d --build backend shopbot
```

## نتیجهٔ اجرا (۲۰۲۶-۱۱-۱۷، ~19:13–19:19 به وقتِ سرور)

- **پوش:** `8dc4755..51c6d17 → origin` (۴ کامیتِ راندها + برگه‌ها).
- **مرج:** سرور `--ff-only` به `51c6d17` — درختِ tracked تمیز (فقط .env.bakهای untrackedِ قبلی).
- **بکاپ:** `pre-shopbot-rounds-ab-20261117-191348.db`، **۱۳۰٬۲۸۵٬۵۶۸ بایت**، در `/app/data/manual_backups/` (API رسمیِ sqlite `.backup`، WAL-safe).
- **backend:** `docker compose up -d --build backend` → Up؛ اسموکِ تصویر سبز (هر ۴ ثابتِ جدید + نبودِ «کسرشی»/«همینجا»/«بدهیِ صورت‌حسابی» در سورسِ تصویر + import موفقِ `routers.shop` و `shop_service` + ثابتِ دکمه در shop_service)؛ اسکنِ لاگِ ۲دقیقه‌ای: صفر خطا.
- **shopbot:** `docker compose up -d --build shopbot` → Up؛ اسموکِ تصویر سبز (`USERNAME_INVALID` موجود — تا دیروز AttributeError؛ مثالِ درستِ `ASK_PREFERRED_NAME`؛ `debt_receipt_done` با واژگانِ درست؛ `handlers.shop` ایمپورت)؛ لاگ: `Application started` + getUpdates 200 — پولینگِ زنده.
- **دست‌نخورده‌ها:** bot / delegate_bot / receipts_bot / frontend همچنان Up (۳هفته) — rebuild نشدند، طبق برگه.
- **باقی‌مانده برای مالک:** اسموکِ ۷مرحله‌ایِ تلگرامی با اکانتِ دورریز (بخشِ مربوط در برگهٔ 11-06) — روی دستِ مالک است، مسدودکننده نیست.
- **Rollback:** همان دستورِ بالا (کد → `8dc4755`، بدون هیچ برگشتِ داده؛ بکاپِ بالا هم برای هر سناریویِ DB موجود است).
