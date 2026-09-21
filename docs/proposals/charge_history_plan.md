# Plan — Account Charge History (read-only)

وضعیت: فاز ۱ (خواندن) تمام شد. این فایل حافظهٔ کاری سشن است — هر چند فاز به‌روز می‌شود.

## تصمیم‌های پایه (برای D13+ در DECISIONS.md)

- «شارژ» = `LedgerEntry(type=charge, account_id ∈ انتخاب)` در بازه؛ credit جدا و فقط با `include_credits=1`.
- حجم افزوده‌شده = `QueuedPlan(status='activated')` (ساخت‌یافته) + `AccountEvent` فقط به‌عنوان marker با tooltip متن (بدون parse عدد).
- `gb_amount IS NULL` = «—»؛ جمع GB فقط روی ردیف‌های معلوم (`charged_gb_known`، None وقتی هیچ ردیف معلومی نیست — قرارداد `BalanceRead.gb_charged`).
- summary همیشه از کل ردیف‌های بازه حساب می‌شود؛ `include_credits=false` فقط لیست `entries` را فیلتر می‌کند نه جمع‌ها را.
- زمان‌ها naive UTC در DB؛ در پاسخ API صریحاً `+00:00` می‌چسبد (خروجی ISO با Z).
- پالت ۸ رنگ categorical (ادامهٔ validated palette سبد ۱–۲ RevenueChart): light/dark جفت.
  1. `#2a78d6`/`#3987e5` 2. `#1baf7a`/`#199e70` 3. `#b26a00`/`#e69f00` 4. `#6a51a3`/`#9e86c8`
  5. `#b0417a`/`#d16ba5` 6. `#007681`/`#2ab5ac` 7. `#8c6d31`/`#b08d57` 8. `#4a5fc1`/`#7b8ce0`
- نمایش شمسی با `react-date-object` (وابستگی موجود `react-multi-date-picker`)، نه کتابخانهٔ جدید.

## فایل‌ها

**Backend (MEDIUM):** جدید `backend/app/routers/history.py` (`GET /api/history/charges`)؛
schemas جدید در `backend/app/schemas.py` (HistoryAccount/HistoryEntry/HistoryPackage/HistoryMarker/HistorySummary/ChargeHistoryResponse)؛
ثبت در `backend/app/main.py`. بدون migration، بدون نوشتن.

**Frontend (LOW):** جدید `frontend/src/components/history/{AccountPicker,RangePicker,ChargeTimeline,CumulativeChart,ChargeTable,SummaryTiles}.tsx`،
صفحهٔ `frontend/src/pages/ChargeHistoryPage.tsx` (`/history`)، `historyApi` در `lib/api.ts`، تایپ‌ها در `lib/types.ts`،
route در `App.tsx`، nav بعد از Finance در `AppShell.tsx`.

**تست:** `backend/tests/test_charge_history.py` (۹ گروه پرامپت §۵) + لنگر زنده با `VPN_HISTORY_DB`.
**Browser:** backend محلی (lifespan off + require_auth override + fake marzban) روی کپی migrate‌شدهٔ `vpn_live_20260920_2348.db` → Playwright.

## endpoint — قرارداد نهایی

`GET /api/history/charges?account_ids=3,46,57,64&since=…&until=…&include_credits=false`

- `account_ids`: رشتهٔ comma؛ خالی/غیرعددی ⇒ 400؛ تکرار dedup؛ >50 ⇒ 400؛ ناموجود ⇒ 404 + فهرست missing.
- `since/until`: مثل `/api/ledger/balance` aware→naive UTC؛ `until` تاریخ ساده ⇒ کل آن روز (23:59:59.999999)؛
  `since>until` ⇒ 400؛ پیش‌فرض ۱۸۰ روز اخیر.
- پاسخ: `accounts` (همیشه برای همهٔ idها، حتی بی‌ردیف) + `entries` + `packages` + `markers` (اکشن‌های
  external_data_limit_increase|adjust|external_usage_reset|payg_cap_hit_reset|settle_reset|deleted_from_marzban) +
  `summaries` (کلید = str(account_id)) + `totals`.
- هر جدول یک کوئری IN؛ جمع‌ها از ردیف‌های همین بازه (نه MoneyBook — این تاریخچهٔ پنجره است نه مانده).

## وضعیت فازها

- [x] فاز ۱: خواندن (AGENTS, DOMAIN §1/§3, D1–D12, models, schemas, ledger/reports routers, تست‌ها, DESIGN, RevenueChart, BalanceSinceControl, HistorySection, AppShell/App, api/types)
- [ ] فاز ۲: backend + تست‌ها
- [ ] فاز ۳: فرانت (Picker/Range → Table → Timeline → Cumulative → Tiles → صفحه)
- [ ] فاز ۴: مرورگر واقعی
- [ ] فاز ۵: مستندات
- [ ] فاز ۶: سه دور بازبینی → docs/audits/2026-09-21_charge_history_review.md

## یافته‌های مهم خواندن

- `parseDate` فرانت رشتهٔ naive را با `+Z` UTC می‌خواند — سازگار با DB.
- DatePicker باید الگوی unwrap Rolldown در `BalanceSinceControl` را کپی کند (`(RawDatePicker as …).default`).
- `docs/from-cursor-repo/` untracked متعلق به ایجنت دیگر — هرگز add نمی‌شود؛ همهٔ `git add` با مسیر صریح.
- suite پایه: ۲۲ backend + ۴ bot سبز — مبنای مقایسه.
