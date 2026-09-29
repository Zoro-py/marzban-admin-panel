# دور ممیزی ۲۰۲۶-۰۹-۲۹ — حوزهٔ ۲: UI/UX پنل ادمین

> چک‌لیست مرجع: `GLM/PROMPT_2026-09-29_full_panel_checklist_sweep.md` §۲.۲ — شاخهٔ `fix/full-checklist-2026-09-29`.
> **تقسیم کار با فاز ۳ (طبق خود پرامپت):** کدِ این حوزه (date-picker سراسری + دوزبانه + هارنس) در همین دور ساخته، تست و کامیت شد؛ سوییپ مرورگریِ کامل (U1-MOBILE، R3 سورت/فیلتر، R9 copy-summary، دسترس‌پذیری) طبق «فاز ۳: آخرین کار قبل از گزارش» بعد از فرود تغییرات حوزه‌های ۲.۳ تا ۲.۷ اجرا می‌شود — وگرنه باید دوبار اجرا می‌شد.

## کاری که در این دور انجام شد

| ردیف | شدت | شاهد اجرا | وضعیت |
|---|---|---|---|
| **U1-MOBILE — هارنس گم‌شده** | زیرساخت تست | `scripts/panel_harness.py` جدید: اپ واقعی FastAPI + دیست فرانت واقعی روی یک پورت، با `lifespan="off"` (هیچ schedulerی — sync/backup/settlement/nudge اصلاً وجود ندارند)، `require_auth` override، و fake Marzbanِ ضبط‌کننده که **روی خود singleton واقعی** بایند می‌شود (نه attribute ماژول — که بی‌صدا از مسیر routerها جا می‌ماند؛ در اولین اسموک همان‌طور لو رفت و درست شد). SPA fallback برای deep-linkها (BrowserRouter). اسموک زندهٔ امروز روی کپی زنده: `/` و `/accounts` = 200، login واقعی JWT می‌دهد، `/api/reports/summary` با دادهٔ زنده پاس می‌دهد | ✅ ساخته شد — ریشهٔ «هارنس نگه‌داری نشده» همین بود که هیچ‌وقت فایلی نبود؛ الان دائمی است |
| **DATE-PICKER سراسری** (باقی‌ماندهٔ D8) | MEDIUM | بک‌اند: پارامتر اختیاری `since` روی هر سه لیست (`GET /api/accounts`، `/api/customers`، `/api/groups`) → یک `MoneyBook(since)` برای کل لیست در یک پاس (نه N+1) → فیلدهای additive ‏`balance_since`/`net_owed_since` (و `payer_balance_since` برای اکانت). فرانت: `SincePicker.tsx` مشترک (پریست‌های ماه شمسی/۳۰/۹۰ + تقویم دومنظورهٔ Jalali/Gregorian + پاک‌کن) بالای `AccountsPage`/`CustomersPage`/`GroupsPage`؛ state در URL (`?since=`) تا ویو shareable بماند؛ ستون «Owes now» در حالت فعال «Owed since» می‌شود و تولتیپ تفکیک «صورت‌حساب‌شدهٔ بازه + pending جاری» می‌دهد — **همان داماسنتیک D13-BS** (pending ماهیتاً window-blind است). راستی‌آزمایی پولی روی کپی زندهٔ امروز: عدد هر سه اسکوپِ رندوم **عددبه‌عدد** برابر خروجی endpoint اثبات‌شدهٔ `/api/ledger/balance?since=` (اکانت/مشتری/گروه)؛ `since=2000` روی همهٔ ردیف‌ها == بی‌پنجره؛ بدون `since` فیلدها None و پاس‌ها د_approximately byte-level همان شکل قبلی. کل ۳۰ فایل تست بک‌اند سبز؛ `tsc -b` + vite build تمیز | ✅ اجرا شد — اثر پولی: صفر (فیلد نمایشی additive از همان MoneyBook اثبات‌شده) |
| **زبان دوزبانه (فارسی/انگلیسی)** | LOW | راستی‌آزمایی اول: هیچ مکانیزم i18nای وجود نداشت (grep ‏i18n/useTranslation/translations = صفر) — پنل انگلیسی-فقط بود. `lib/i18n.tsx` جدید: hook کوچک `useSyncExternalStore`-محور + `tr(lang, fa, en)`؛ پیش‌فرض `en` (وضع موجود)؛ کلید `vpn_dashboard_lang` در localStorage (حافظهٔ انتخاب، طبق چک‌لیست). دکمهٔ toggle در هدر AppShell کنار theme. اعمال روی متن‌های پرکاربردترین: **BalanceSinceControl کامل** (برچسب، تفکیک صورت‌حساب‌شده/نشده، carried-over note، خلاصهٔ GB با تولتیپش، clear و hint)، **صفحهٔ History** (تیترها، توضیح‌ها، خطاها، empty state‌ها، Show payments، کارت‌های timeline/cumulative)، و **RangePicker مشترک** (پریست‌ها + کپشن — Finance هم از آن استفاده می‌کند). build تمیز | ✅ اجرا شد — دامنهٔ عمداً حداقلی؛ زیرکامپوننت‌های عمیق تاریخچه (ChargeTable و…) هنوز انگلیسی‌اند، عمداً و ثبت‌شده |
| R3 (سورت/فیلتر همهٔ ستون‌ها) | — | کد سورت در همین دور دست نخورد (SortableHeader + compareBy خوانده و سالم)؛ کلیک‌آزمایی مرورگری → فاز ۳ | ⏳ فاز ۳ |
| R9 (copy-summary گروه) | — | → فاز ۳ (کلیک + مقایسهٔ کلیپ‌بورد با API) | ⏳ فاز ۳ |
| دسترس‌پذیری پایه | — | → فاز ۳ (Tab کامل روی هر صفحه با هارنس) | ⏳ فاز ۳ |

## ناوردایی‌های قبل از کامیت

- بک‌اند ۳۰/۳۰ فایل تست سبز (شامل تست جدید IDOR حوزهٔ ۱).
- `py_compile` روی هر ۵ فایل بک‌اند تغییریافته.
- `tsc -b` + `vite build` تمیز.
- متقاطع‌چک عددی since با endpoint مرجع روی کپی زنده (بالا).

## کامیت‌ها

- `feat(panel): list-wide ?since= window …` — بک‌اند + فرانت date-picker (این کامیت).
- `feat(panel): minimal FA/EN layer for Balance-since/History …` — i18n (کامیت جدا، LOW از MEDIUM).
- `feat(scripts): panel UI harness …` — هارنس (کامیت جدا).
