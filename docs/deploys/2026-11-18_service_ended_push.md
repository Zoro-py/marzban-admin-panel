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

## نتیجهٔ اجرا

- (پس از اجرا ثبت می‌شود.)
