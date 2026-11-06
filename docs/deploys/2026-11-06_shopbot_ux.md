# 2026-11-06 — دیپلوی بازسازی UX شاپ‌بات (راند ۱ + راند ۲ ریویو)

> **وضعیت: NOT APPLIED — اجرا فقط با GO مالک.** این برگه فقط برنامهٔ استقرار است؛
> هیچ پوش/SSH/دیپلوی تا این لحظه انجام نشده است.

- **شاخه:** `feat/shopbot-ux-rebuild` — ۱۸ کامیت جلوتر از `2b57db2` (راند ۱: `e7de994..7ae82a6`؛ فیکس‌های راند ۲: `724c300` R1، `b861a0b` R2، `e67f86a` R3، `08bddf0` R4). **UNPUSHED** — پیش‌نیازِ هر دیپلوی: پوشِ شاخه با GO مالک.
- **مستندات راند ۲:** `docs/ZCODE_SHOPBOT_REVIEW.md` (R1+R3)، `docs/ZCODE_SHOPBOT_COPY_REVIEW.md` (R2)، `docs/SHOPBOT_SCREENS.md` (R3)، ورودی‌های R1–R4 در `docs/ZCODE_PROGRESS.md`.

## آنچه سوار می‌شود

| سرویس | تغییر | migration/DDL |
|---|---|---|
| `backend` | فیلدهای فقط-خواندنیِ سشن (`has_accounts`/`has_payg_service`)، `renews_here` در لیست سرویس‌ها، تحویلِ یک‌پیامی (QR+کپشن+دکمهٔ URL)، فیکسِ آرگومان‌های `_alert_operator_to_topup`، واژگانِ جدیدِ متن‌های پولی + سه رشتهٔ رله‌شونده (R1) | **هیچ** — فقط فیلدهای response؛ DB دست نمی‌خورد |
| `shopbot` | منوی ۴-دری، «حساب من»، خرید ۳تپ، کارت‌های سرویس، واژگان واحد، بن‌بست‌ها، فیکس‌های R1 (`handle_other` NameError، پرچم `vol:…:f`، منو زیرِ «انصراف») | — |
| `bot` / `delegate_bot` / `frontend` | **دست‌نخورده** — هیچ کامیتِ این شاخه به آن‌ها نمی‌خورد | — |

## ترتیب: اول backend، بعد shopbot (تست‌شده، دو جهت)

- **رباتِ جدید + بک‌اندِ قدیم** (`shopbot/test_compat_deploy.py`، ۸ چک سبز): هیچ کرشی نیست؛ فقط تنزلِ مستند — برچسبِ منو «خرید» ساده می‌شود، صفحهٔ خرید دکمهٔ «سرویس جداگانه» را از دست می‌دهد و دکمهٔ تمدید روی همهٔ کارت‌ها برمی‌گردد (رفتارِ قبل از `0aa626d`؛ تمدید همچنان همان `renewable_account` را تمدید می‌کند). همین‌ها دلیلِ ترتیب‌اند.
- **رباتِ قدیمی + بک‌اندِ جدید** (`temp/r5_compat/run_oldbot_compat.py` روی کدِ واقعیِ `2b57db2`، ۱۰ چک سبز): پنجرهٔ بینِ دو دیپلوی کاملاً سالم است — چت‌های قدیمی همهٔ مسیرهایشان جواب می‌گیرند؛ فیلدهای جدید نادیده گرفته می‌شوند؛ پیامِ تحویلِ یک‌پیامی مستقیم از بک‌اند به تلگرام می‌رود و به پروسهٔ ربات کاری ندارد. **رفتارِ قدیمیِ شناخته‌شده در این پنجره:** تپِ «انصراف» روی کیبوردِ بدهیِ قدیمی، جریانِ رسید را باز می‌کند (باگِ قدیمی — فیکسش در رباتِ جدید است، 6267df5) و در خرید، سؤالِ نام کاربری روی این بیس وجود ندارد.

## پیش‌نیازها (روی laptop، قبل از هر SSH)

1. GO مالک → `git push origin feat/shopbot-ux-rebuild`
2. هر ۱۰ سوییت ربات + ۴ سوییتِ بک‌اندِ مرتبط سبز باشند (آخرین اجرا: راند ۲ — همه سبز).

## مرحلهٔ ۱ — backend (روی france-vpn)

```bash
cd /opt/marzban-admin-panel
git fetch && git checkout feat/shopbot-ux-rebuild

# بکاپِ sqlite با API رسمی (در WAL-safe است، برخلاف cp خام):
docker exec marzban-admin-panel-backend-1 python - <<'PY'
import sqlite3, time
dst = f"/app/data/manual_backups/pre-shopbotux-{time.strftime('%Y%m%d-%H%M%S')}.db"
src = sqlite3.connect("/app/data/vpn.db")
dst_c = sqlite3.connect(dst)
src.backup(dst_c)
dst_c.close()
ok = sqlite3.connect(dst).execute("PRAGMA integrity_check").fetchone()[0]
n = sqlite3.connect(dst).execute("SELECT COUNT(*) FROM ledgerentry").fetchone()[0]
print(f"backup={dst} integrity={ok} ledger_rows={n}")
PY

docker compose up -d --build backend
docker compose logs backend --since 2m | tail -40        # بدون Traceback
curl -fsS http://127.0.0.1:8010/api/health                # 200
```

اسموکِ بک‌اند (بدون مشتریِ واقعی): با یک اکانتِ تستِ دورریز (اگر اکانت تستِ از-قبل ساخته‌شده هست از همان؛ وگرنه یک ShopUser تستی از پنل):

```bash
# <TEST_TELEGRAM_ID> = شناسهٔ عددیِ اکانتِ تستِ دورریز (از پنل، ستونِ Telegram ID)
docker exec -i -e TEST_TELEGRAM_ID=<TEST_TELEGRAM_ID> -w /app marzban-admin-panel-backend-1 python - <<'PY'
# فقط-خواندنی: سشنِ کاربرِ تست حالا دو فیلدِ جدید را برمی‌گرداند؟
import asyncio, os, httpx
async def main():
    async with httpx.AsyncClient(base_url="http://localhost:8000",
                                 headers={"X-Shop-Bot-Key": os.environ["SHOP_BOT_API_KEY"]}) as c:
        r = await c.post("/api/shop/bot/session", json={"telegram_id": int(os.environ["TEST_TELEGRAM_ID"])})
        r.raise_for_status()
        s = r.json()
        assert {"has_accounts", "has_payg_service"} <= set(s), s.keys()
        print("session OK:", {k: s[k] for k in ("has_accounts", "has_payg_service", "ledger_debt")})
asyncio.run(main())
PY
```

## مرحلهٔ ۲ — shopbot (بعد از سبزِ اسموکِ مرحلهٔ ۱)

```bash
docker compose up -d --build shopbot
docker compose logs shopbot --since 2m | tail -30        # «Shop bot starting»، بدون Traceback
```

## اسموکِ نهایی با اکانتِ تست (در تلگرام — هرگز با مشتریِ واقعی)

از اکانتِ تستِ دورریز، به‌ترتیب:

1. `/start` → خوش‌آمد + منوی ۴-دری (برای مشتریِ بدون سرویس: «تست رایگان» + «خرید» + «راهنما»).
2. «🛒 خرید سرویس» → صفحهٔ حجمِ inline → تپِ یک حجم → رسیدِ کارت‌به‌کارت با منو زیرِ «انصراف» → «❌ انصراف» → «لغو شد».
3. «📱 سرویس‌های من» → کارت با «🔄 تمدید / 🔗 لینک اشتراک / ⋯ بیشتر» — تمدید فقط روی کارتی که `renews_here` است (در مشتریِ چند-سرویسی).
4. «💳 حساب من» → نمای بدهی/موجودی؛ «ارسال رسید کارت‌به‌کارت» → «همین مبلغ» → پرامپتِ رسید → «انصراف».
5. (اگر موجودیِ تست کافی شد) خریدِ کیف‌پولی: تأییدِ یک‌تپی → تحویلِ **یک‌پیامی**: عکسِ QR + کپشن (لینک در خطِ مستقل) + دکمهٔ «🔗 لینک اشتراک».
6. یک ویس/استیکر بفرست → باید «متوجه نشدم 🙏» بیاید (فیکسِ R1-1 — قبلاً خطای فنی می‌داد).
7. دکمهٔ inline قدیمی (رسید/هشدارِ قبلی) را تپ کن → «این دکمه قدیمی شده — …»، هرگز سکوت.

## Rollback

هیچ migration و هیچ نوشتنِ داده در کار نیست — rollback = برگشتِ کد و rebuildِ همان سرویس:

```bash
# برگشتِ هر دو سرویس به بیسِ قبل از راند ۱ و ۲:
cd /opt/marzban-admin-panel
git checkout 2b57db2
docker compose up -d --build backend shopbot
```

- اگر فقط shopbot مشکل دارد: `git checkout 2b57db2 -- shopbot/ && docker compose up -d --build shopbot` (بک‌اندِ جدید با رباتِ قدیمی تست‌شده و سالم است).
- اگر فقط backend مشکل دارد: `git checkout 2b57db2 -- backend/ && docker compose up -d --build backend` (رباتِ جدید با بک‌اندِ قدیمی تست‌شده و «کار می‌کند» ولی تنزل‌های مستندِ بالا را دارد).
- بازگشتِ داده (فقط اگر کوچک‌ترین شکی به خرابیِ DB): فایلِ بکاپِ `manual_backups/pre-shopbotux-*.db` — با توقفِ backend و جایگزینیِ کنترل‌شده؛ این سناریو انتظار نمی‌رود (هیچ نوشدنیِ جدیدی در این شاخه به DB نیست).

## موارد باز مرتبط (بعد از دیپلوی هم باز می‌مانند)

- M-1 هدفِ per-account برای تمدید چند-سرویسی و M-2 اعتبارسنجیِ زمانِ تپِ دکمهٔ تمدیدِ کهنه (هر دو «OWNER/CLAUDE MUST DECIDE» در `ZCODE_SHOPBOT_REVIEW.md`) — تغییر بک‌اند می‌خواهند، در این دیپلوی نیستند.
- ۵ سؤالِ بازِ مالک از پایانِ راند ۱ (برچسب «خرید / تمدید»، +۱ تپِ شارژ و …) — `ZCODE_PROGRESS.md` پایانِ راند ۱.
