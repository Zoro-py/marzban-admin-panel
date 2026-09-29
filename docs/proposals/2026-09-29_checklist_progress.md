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
| ۲.۶ | گروه‌ها | ✅ تمام — reset-cycle P1 رفع شد؛ D21 استرس سبز | `2026-09-29_round_06_groups.md` |
| ۲.۷ | auto-renew | ✅ تمام — سوییچ ردیفی + batch + confirm | `2026-09-29_round_07_autorenew.md` |
| ۲.۸ | بقیه | ✅ تمام — گارد ardani + fail2ban/S1/CI/restore-test | `2026-09-29_round_08_misc.md` |

## کامیت‌های این دور

- `f4225cb` test(delegate): IDOR hostile suite (حوزهٔ ۲.۱)
- `0e850aa` feat(scripts): panel UI harness (حوزهٔ ۲.۲)
- `dda90d5` feat(panel): list-wide ?since= window (DATE-PICKER، حوزهٔ ۲.۲)
- `5b17be1` feat(panel): minimal FA/EN layer (حوزهٔ ۲.۲)
- `87a0e92` fix(billing): D8 sync↔settle billing lock (حوزهٔ ۲.۳)
- `b6e2a8b` docs(audit): round 03
- round 04/05 audits
- `12c1443` fix(billing): reset-cycle refuses non-payg (حوزهٔ ۲.۶)
- `4b4c8f1` test: D21 toggle stress
- `3b7a69b` docs(audit): round 06
- `9d434e7` feat(frontend): auto-renew simplification (حوزهٔ ۲.۷)
- `0f53102` docs(audit): round 07
- ardani zero-charge guard fix+test (حوزهٔ ۲.۸)
- `docs(audit)` round 08
