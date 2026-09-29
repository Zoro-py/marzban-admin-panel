# دور ممیزی ۲۰۲۶-۰۹-۲۹ — حوزهٔ ۷: تمدید خودکار + «ساده‌سازی» (درخواست مالک)

> چک‌لیست مرجع: `GLM/PROMPT_2026-09-29_full_panel_checklist_sweep.md` §۲.۷ — شاخهٔ `fix/full-checklist-2026-09-29`.

## وضعیت قبل از این دور (راستی‌آزمایی)

- R20 (خاموش‌کردن per-account و batch در bulk-creation): فیلد `auto_renew_enabled`، PATCH ‏`/api/accounts/{id}/billing` و پیش‌فرض bulk هر سه موجود و تست‌پوش — **تأیید سریع مجدد، سالم**.
- toggle تنها داخل `AccountInspector → BillingSection` بود (با کل فرم billing ذخیره می‌شد)؛ جدول فقط بَجِ «no auto-renew» برای حالت خاموش داشت؛ هیچ batch-toggle از جدول ممکن نبود؛ هیچ تأییدِ قبل از خاموش‌کردن وجود نداشت.

## بازطراحی اجراشده (چهار معیار مالک)

| معیار | پیاده‌سازی |
|---|---|
| ۱) toggle از خود ردیف جدول | کامپوننت جدید `AutoRenewSwitch.tsx`: سوییچ ردیف‌سطح در ستون «Auto-renew» جدول اکانت‌ها؛ همان PATCH بک‌اند (فقط این فیلد در body، زیر `serialise_billing`؛ رد `stopPropagation` تا بازشدن inspector با کلیک سطر قاطی نشود) |
| ۲) نشانهٔ بصری وضعیت روی خود ردیف | خودِ سوییچ حالت روشن/خاموش را در هر دو حالت نشان می‌دهد (قبلاً فقط حالت خاموش بَج داشت)؛ بَج «no auto-renew» با تولتیپش سر جایش ماند |
| ۳) عملیات batch از جدول | انتخاب ردیف‌ها تعمیم یافت (قبلاً فقط ردیف‌های بدهکار checkbox داشتند — کامنت قدیمی «selection exists purely to drive bulk settle» به‌روز شد)؛ نوار انتخاب (`BulkSettleBar`) حالا دو دکمهٔ «Auto-renew off / on» برای همهٔ انتخاب‌شده‌ها دارد — حلقهٔ ترتیبی client-side روی همان PATCH تک‌اکانتی (همان قرارداد حلقهٔ settle؛ بدون endpoint جدید که بخواهد اعتماد پولی از صفر بگیرد) |
| ۴) تأیید قبل از خاموش‌کردن | هر دو مسیر OFF (تکی و batch) اول `window.confirm` می‌پرسند — batch لیست usernamها (تا ۸ + شمار باقی) را نشان می‌دهد؛ مسیر ON بی‌تأیید است چون جهت امن است (فقط ورود دوباره به صف) |

## راستی‌آزمایی اجرا

- `PATCH /billing` با فقط `auto_renew_enabled=false`: ۲۰۰، فیلد در DB عوض شد، ‏`billed_data_limit` دست نخورد (گارد سوم D21 محاسبه کرد مود مؤثر فلِیپ نکرده → no-op)، ردیف `AccountEvent(billing_change)` با ‏`auto_renew_enabled=False` در detail ثبت شد — یعنی تغییر از جدول هم audit-trail دارد.
- `tsc -b` + vite build تمیز.
- اسکرین‌شات‌های قبل/بعد (خواستهٔ چک‌لیست) → فاز ۳ با هارنس Playwright (بعد از اینکه همهٔ تغییرات UI دیگر حوزه‌ها هم فرود آمدند، یک‌جا گرفته می‌شوند).
- رفتار خودکار: هیچ کدی در مسیر صف (`_maybe_queue_next_plan`/`_EXCLUDED_STATUSES`/opt-out) دست نخورده — تست‌های موجود `test_auto_queue_dampening` و R20 سبز ماندند (کل ‏۳۳/۳۳ فایل بک‌اند سبز).

## اثر پولی

صفر — این UI فقط پرچم opt-out اپراتوری را می‌نویسد؛ هیچ مسیر شارژ/credit جدیدی وجود ندارد و مسیرهای settle/queue بایت‌به‌بایت همان قبلی‌اند.
