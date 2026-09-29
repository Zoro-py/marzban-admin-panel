# پیشرفت چک‌لیست جامع ۲۰۲۶-۰۹-۲۹

> شاخهٔ `fix/full-checklist-2026-09-29` — مرجع: `GLM/PROMPT_2026-09-29_full_panel_checklist_sweep.md`.
> این فایل هر ۲۵-۳۰ تماس ابزار به‌روز می‌شود تا اگر بودجه تمام شد، کار تا همان‌جا قابل‌استفاده باشد.

## Baseline (فاز ۰) — ثبت‌شده قبل از هر تغییر

- backend: **۲۹/۲۹ فایل تست سبز** (اجرا standalone مثل CI، نه pytest collect — سه فایل script-style اند).
- bot: **۵/۵**، delegate_bot: **۲/۲** (با env همسان CI).
- فرانت: `npm run build` تمیز (1.12s).
- ناوردایی پول روی کپی زندهٔ امروز (`livecopy_0929.db`، بکاپ آنلاین sqlite از france): Σ لجر = **۱۴٬۸۵۳٬۹۰۰٫۱۶ت**، صفر ردیف بی‌مالک، همهٔ bucketها با SQL خام برابر.

## وضعیت حوزه‌ها

| # | حوزه | وضعیت | فایل ممیزی |
|---|---|---|---|
| ۲.۱ | سه بات | ✅ تمام — صفر یافتهٔ جدید؛ DEL-1 از قبل روی main رفع بوده؛ تست IDOR دائمی جدید | `docs/audits/2026-09-29_round_01_bots.md` |
| ۲.۲ | UI/UX پنل | ⏳ | `2026-09-29_round_02_frontend.md` |
| ۲.۳ | محاسبات مالی | ✅ تمام — D8 اجرا شد (تست قرمز → رفع → سبز)؛ اسکن D21 صفر | `2026-09-29_round_03_money.md` |
| ۲.۴ | میانگین مصرف | ✅ تمام — فلیت تمیز، هر دو follow-up در داده موجود نیست | `2026-09-29_round_04_usage.md` |
| ۲.۵ | نمودارها | ✅ بک‌اند/داده تمام؛ رندر مرورگری → فاز ۳ | `2026-09-29_round_05_charts.md` |
| ۲.۶ | گروه‌ها | ⏳ | `2026-09-29_round_06_groups.md` |
| ۲.۷ | auto-renew | ⏳ | `2026-09-29_round_07_autorenew.md` |
| ۲.۸ | بقیه | ⏳ | `2026-09-29_round_08_misc.md` |

## کامیت‌های این دور

- `f4225cb` test(delegate): IDOR hostile suite (حوزهٔ ۲.۱)
- `0e850aa` feat(scripts): panel UI harness (حوزهٔ ۲.۲)
- `dda90d5` feat(panel): list-wide ?since= window (DATE-PICKER، حوزهٔ ۲.۲)
- `5b17be1` feat(panel): minimal FA/EN layer (حوزهٔ ۲.۲)
- D8 fix billing lock (حوزهٔ ۲.۳) — همین کامیت
