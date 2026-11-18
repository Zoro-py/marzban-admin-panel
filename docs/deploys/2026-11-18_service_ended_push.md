# 2026-11-18b — دیپلویِ pushِ پایانِ سرویس («سرویست قطع شد»)

> **وضعیت: با GO مالک اجرا می‌شود.** الگوی کامل در [`2026-11-17_shopbot_rounds_ab.md`](2026-11-17_shopbot_rounds_ab.md).

## آنچه سوار می‌شود

| سرویس | تغییر | migration/DDL |
|---|---|---|
| `backend` | pushِ پایانِ سرویس در همان سوییپِ هشدارها: تمام‌شدنِ حجم/زمان (و اتمامِ تریال) → یک پیامِ آرام با دکمهٔ تمدیدِ همان حساب؛ یک‌بار در هر رویدادِ پایان (`ended_warned_at`) | **یک ستونِ افزودنیِ nullable:** `shoporder.ended_warned_at DATETIME` — **خودکار** در startup با الگوی `PRAGMA/ALTER` خودِ `db.py`؛ هیچ قدمِ دستیِ اپراتور؛ rollback کد ستون را رها می‌کند (بی‌اثر و بی‌ضرر) |
| `shopbot` | **دست‌نخورده** — push از بک‌اند با کال‌بک‌های موجودِ ربات می‌آید (`ren:/renew:` مهرزده) | — |

**کامیت:** `27e1578` — قرمزِ اندازه‌گیری‌شده (۱۱ فیل) → سبز؛ ۱۴ سوییت بک‌اند + ۱۵ ربات.

## ترتیب و مراحل

فقط backend rebuild می‌شود. پوش → بکاپ sqlite → `--ff-only` → `docker compose up -d --build backend` → اسموک:

```bash
docker exec marzban-admin-panel-backend-1 python - <<'PY'
import sqlite3
cols = {r[1] for r in sqlite3.connect("/app/data/vpn.db").execute("PRAGMA table_info(shoporder)")}
assert "ended_warned_at" in cols, cols
from app import shop_texts
assert callable(shop_texts.service_ended) and callable(shop_texts.trial_ended)
src = open("/app/app/shop_service.py", encoding="utf-8").read()
assert 'mark = "ended"' in src
print("backend image OK (column + texts + sweep branch)")
PY
```

اسموکِ تلگرامیِ دستِ مالک: با اکانتِ دورریز، سرویس را تا ۱۰۰٪ مصرف کنید (یا expire را در پنل دستی به گذشته بگذارید) → باید pushِ «🔴 … قطع شد» با دکمهٔ تمدید بیاید و **دوباره نیاید**.

## Rollback

```bash
cd /opt/marzban-admin-panel
git checkout b1a7663 -- backend/
docker compose up -d --build backend
# ستونِ اضافه‌شده روی DB می‌ماند (nullable، بی‌استفاده از کدِ قدیم — بی‌ضرر)؛ بکاپِ قبل از دیپلوی موجود.
```

## نتیجهٔ اجرا (۲۰۲۶-۱۱-۱۷ ~22:33 به وقتِ سرور)

- **پوش + مرج:** `8ae2311..01f069d → origin`؛ سرور `--ff-only` به `01f069d` — درخت تمیز.
- **بکاپ:** `pre-ended-push-20261117-223306.db` (API رسمی، WAL-safe).
- **backend rebuild:** Up؛ اسموک سبز — ستونِ `ended_warned_at` **واقعاً روی DBِ زنده ساخته شد** (migration خودکار در startup)، `service_ended`/`trial_ended` و شاخهٔ `mark = "ended"` در تصویر، روترها import؛ اسکنِ لاگ صفر خطا.
- **shopbot:** دست‌نخورده (Up از دیپلوی قبلی) — pushها با کال‌بک‌های موجودِ ربات می‌آیند.
- **باقی‌مانده برای مالک:** اسموکِ تلگرامی (مصرفِ ۱۰۰٪ یا گذاشتنِ دستیِ expire در گذشته با اکانتِ دورریز → یک push «قطع شد» با دکمهٔ تمدید، و تکرارنشدن).
- **Rollback:** `git checkout b1a7663 -- backend/ && docker compose up -d --build backend` (ستون روی DB می‌ماند — nullable و بی‌استفاده از کدِ قدیم؛ بکاپ موجود).

## Fix round 1 (همان روز، بلافاصله پس از گزارشِ زندهٔ مالک)

- **گزارشِ مالک:** «و اصلا قابل فهم نیست کدوم اکانت تموم شده» — پیام فقط حجم را می‌گفت؛ دو سرویسِ هم‌حجم قابلِ تشخیص نبودند.
- **فیکس (`45955a9`):** لقبِ خودِ مشتری (`Account.service_name`) **سرِ پیام** می‌نشیند: «🔴 سرویس «گوشی علی» قطع شد — حجمش تمام شد. / حجم این سرویس: ۳۰ گیگ…»؛ سرویسِ بی‌لقب: پیام می‌گوید دکمهٔ تمدید **همین** سرویس را وصل می‌کند و به «📱 سرویس‌های من» اشاره می‌کند (کارتِ قطع، وضعیت را نشان می‌دهد).
- **دیپلوی شد (2026-11-18 ~08:14):** بکاپ `pre-ended-push-fix1-20261118-081400.db` → ff به `6fe5f9d` → rebuild backend → اسموک سبز («گوشی علی» در شاخهٔ نام‌دار؛ «همین سرویسِ قطع‌شده»+«سرویس‌های من» در شاخهٔ بی‌نام)؛ لاگ پاک؛ shopbot دست‌نخورده.
