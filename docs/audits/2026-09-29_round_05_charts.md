# دور ممیزی ۲۰۲۶-۰۹-۲۹ — حوزهٔ ۵: نمودارها

> چک‌لیست مرجع: `GLM/PROMPT_2026-09-29_full_panel_checklist_sweep.md` §۲.۵ — شاخهٔ `fix/full-checklist-2026-09-29`.
> سهم این دور: راستی‌آزمایی بک‌اند/داده — سوییپ رندر مرورگری همهٔ ردیف‌ها یک‌جا در فاز ۳ (بعد از فرود همهٔ تغییرات) اجرا می‌شود.

## جدول یافته‌ها

| ردیف | شاهد اجرا | وضعیت |
|---|---|---|
| History (Timeline/Cumulative/Table) | کد این دور فقط برچسب‌های دوزبانه عوض شد (منطق رندر دست نخورد)؛ `tsc`+build تمیز؛ رندر واقعی → فاز ۳ | ⏳ فاز ۳ |
| Finance + RangePicker مشترک (D22) | endpoint `/api/reports/finance` روی کپی زندهٔ امروز: ۲۰۰، ‏`revenue_this_month` = ‏۸٬۸۷۵٬۳۳۱٫۱۷ت، ‏`revenue_by_day`/`charged_by_day` حاضر، بدون None-leak. تطبیق عدد با History روی همان بازه → فاز ۳ | ✅ بک‌اند / ⏳ فاز ۳ |
| ChargeHistoryPreview (سه mount) | → فاز ۳ | ⏳ فاز ۳ |
| RevenueChart (دشبورد) | منبع داده‌اش همان `/api/reports/finance` است (بند بالا) — با دادهٔ امروز (بعد از رمدیشن دیروز) crash نمی‌کند؛ رنگ/legend → فاز ۳ | ✅ داده سالم / ⏳ فاز ۳ |
| CPU/RAM سرورها (`/servers`, R19) | endpoint `/api/reports/system-status`: ۲۰۰ با شکل درست (`cpu_percent`/`mem_percent`/`mem_used_mb`/`mem_total_mb`/`load_avg_1m=null` روی ویندوز — مستند). **مقایسهٔ زندهٔ SSH با france-vpn (فقط‌خواندنی):** ‏`top` واقعی ~۱۳٫۶٪ CPU busy، رم ۲۰۱۲/۳۹۱۶MB ≈ ۵۱٪، و **%st = ۰٫۰** — یعنی CPU-steal فصل قبل (D-I مرجع §۶ DOMAIN) روی tier Highload هنوز برنگشته. مقایسهٔ عددبه‌عدد با نمودار فقط روی خود سرور معنا دارد (psutil همان host را می‌خواند)؛ شکل/سقف‌ها درست‌اند | ✅ endpoint + دادهٔ سرور سالم |

## جمع‌بندی

- یافتهٔ تازه: **۰**. تغییر کد: هیچ (این حوزه).
- باقی‌ماندهٔ عمدی: رندر مرورگری همهٔ نمودارها → فاز ۳ با هارنس.
