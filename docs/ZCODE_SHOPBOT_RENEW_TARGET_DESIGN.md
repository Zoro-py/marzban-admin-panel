# طراحی تمدیدِ per-account (هدف‌گیری صریحِ سرویس در خرید) — Round 2 · R6

<div dir="rtl">

**فقط طراحی — هیچ کدی در این راند نوشته/اجرا نشده است.** سؤالِ بازِ ④ مالک از راند ۱: در مشتریِ چند-سرویسی، دکمهٔ «تمدید» (و خودِ «خرید») همیشه `renewable_account` را تمدید می‌کند — «آخرین سرویسِ تحویل‌شده»، نه کارتی که رویش تپ شده. این سند آنچه را برای هدف‌گیریِ واقعی لازم است، ریسک‌های پولی و فهرستِ تست را مشخص می‌کند.

## ۱. وضع موجود (همه VERIFIED در کد)

- `ShopOrder` ستونِ هدف ندارد — مدل: `backend/app/models.py` (`class ShopOrder`: shop_user_id, data_limit_gb, duration_days, price, status, account_id، …). هدفِ تمدید در لحظهٔ ساختِ سفارش با `renewable_account(session, shop_user_id)` حل می‌شود (`shop_service.py:1795`): **جدیدترین** سفارشِ delivered با حسابِ زنده و غیر-غیرفعال (تست‌ها هم حسابند؛ فال‌بکِ مشتریِ وصل‌شده از `Customer.shop_user_id`).
- گاردهای فعلی per-CUSTOMERاند نه per-TARGET: `refuse_unlimited_renewal(session, shop_user_id, force_new_service=)` (`shop_service.py:1863`) از `purchase()`، `/quote` و `/orders` صدا می‌خورد و روی «حسابی که خریدِ بعدی تمدید می‌کند» قضاوت می‌کند — دو پیشوندِ پایدار: `PAYG_TOPUP_INSTEAD` (اول، چون capِ payg صفر است و گاردِ بعدی را گمراه می‌کند) و `SERVICE_IS_UNLIMITED` (falsy = None **یا 0** — درسِ F3a). `_extend_order` همان‌ها را روی پنلِ زنده دوباره چک می‌کند (دفاعِ در-عمق).
- endpointهای درگیر: `POST /quote`، `POST /orders`، `POST /orders/{id}/username`، `POST /orders/{id}/pay` (`routers/shop.py:1349/1522/1586/1618`) + `GET /orders/pending`.
- کال‌بکِ فعلی: `renew:{gb}` (فقط حجم) — از هشدارهای بک‌اند و (از 0aa626d) از کارتِ فقط-یکی از سرویس‌ها (فیلدِ `renews_here`).
- سبکِ migration این ریپو: `_run_lightweight_migrations()` در `backend/app/db.py` — `ALTER TABLE … ADD COLUMN …` با گاردِ وجودِ ستون؛ بدون alembic.

## ۲. طراحی پیشنهادی

### ۲-۱. ستون و migration

`ShopOrder.target_account_id: Optional[int]` — `NULL` = معنای فعلی (`renewable_account`). مهاجرت در همان `_run_lightweight_migrations`:

```python
# پس از گاردِ وجودِ ستون، مطابق سبکِ همان تابع:
conn.execute(text("ALTER TABLE shoporder ADD COLUMN target_account_id INTEGER"))
conn.execute(text("CREATE INDEX IF NOT EXISTS ix_shoporder_target_account_id ON shoporder (target_account_id)"))
```

سبکِ موجود را دنبال می‌کند (ALTER ساده، بدون REFERENCES — FK در sqlite پراگماتیکی است و بقیهٔ ALTERهای همین فایل هم بدون REFERENCESاند). nullable و بدون default → ردیف‌های قدیمی و همهٔ مسیرهای قدیمی بلافاصله درست‌اند.

### ۲-۲. حلِ هدف: یک هلپرِ جدید به‌جای پخش‌شدنِ منطق

```python
def resolve_renew_target(session, shop_user_id, target_account_id: Optional[int]) -> Account:
    """target_account_id is None → renewable_account (رفتارِ امروز، برای سازگاری).
    Otherwise the id must be a VISIBLE account of THIS shop user, alive, and
    able to take a volume purchase — otherwise ShopError with the same stable
    prefixes."""
```

- **مالکیت** — بازاستفاده از منطقِ `bot_rename_account` (`routers/shop.py:2208`): visible_ids = حسابِ سفارش‌های deliveredِ این shop_user ∪ حساب‌های مشتری(های) وصل (`linked_customers`). این منطق به یک هلپرِ مشترک (`visible_account_ids(session, shop_user_id)`) استخراج شود تا rename و این مسیر هرگز واگرا نشوند. پاسخِ خطا 404 بدون oracle (همان رویهٔ rename).
- **گاردهای per-TARGET** — همان دو پیشوند، این‌بار روی «همین حساب»: payg-موثر (`effective_billing_mode`) → `PAYG_TOPUP_INSTEAD`؛ cap falsy (None/0) → `SERVICE_IS_UNLIMITED`؛ status ∈ (disabled, deleted_from_marzban) یا `deleted_at` ست → «This service is no longer active.» (مثل `auto_renew_account`). تست‌ رایگانِ درجریان هدفِ مجاز است (تمدیدِ in-place نقطهٔ کلیدیِ trial است — همان قاعدهٔ `renewable_account`).
- **تفاوت با auto-renew:** تمدیدِ پولیِ حسابِ مشتریِ وصل (customer_id ≠ NULL) مجاز است (فال‌بکِ فعلی هم همین را تمدید می‌کند)؛ ولی «تمدید خودکار» (موجودیِ منفی‌شونده) همچنان فقط-حسابِ فروشگاهی می‌ماند — گاردِ فعلیِ `auto_renew_account` دست نمی‌خورد.
- **تعامل با force_new:** `force_new_service=True` + `target_account_id≠NULL` → 400 (دو معنیِ متناقض؛ هرگز با هم).

### ۲-۳. endpointها

- `ShopBotPurchaseRequest` (بدنهٔ `/orders` و `/quote`): فیلدِ اختیاریِ `target_account_id: Optional[int] = None`. هر دو endpoint قبل از هر محاسبه‌ای `resolve_renew_target` را صدا می‌زنند؛ `/quote` labelِ هدف را هم در intent برمی‌گرداند (`target_label` = نامِ نمایشیِ سرویس — همان `_display_service_name` ربات) تا صفحهٔ تأیید بگوید این پول به کدام سرویس می‌رود.
- `/orders/{id}/pay`: **بدون تغییرِ قرارداد** — ولی یک وریفایِ پارانویکِ خطِ پول: قبل از دبت، `order.target_account_id` (اگر ست است) دوباره از visible_ids عبور داده شود؛ تخطی = 404 و **صفر جابه‌جاییِ پول**.
- `GET /accounts`: `renews_here` یک ریلیز می‌ماند (True روی همهٔ کارت‌های قابل-تمدید؛ برای ربات‌های قدیمی‌تر که معنیِ «دکمه روی این کارت» را دارند) و بعد حذف.

### ۲-۴. کال‌بک و سازگاری

- کال‌بکِ جدید: `ren:<account_id>:<gb>` (مثلاً `ren:101:30`) — با `renew:` قدیمی تصادم ندارد (پیشوندِ مجزا) و زیرِ سقفِ 64 بایتِ callback_data. `ren:` بدشکل → همان «این دکمه قدیمی شده» (الگویِ `svcmore:`).
- `renew:{gb}` قدیمی **همچنان** مسیریابی می‌شود و رفتارِ امروز را دارد (هدف = `renewable_account`) — دکمه‌های روی چت‌های قدیمی هیچ‌وقت مرده نمی‌شوند.
- **نکتهٔ سازگاریِ حیاتی:** بک‌اندِ قدیمی فیلدِ ناشناختهٔ `target_account_id` را در pydantic **بی‌صدا نادیده می‌گیرد** — یعنی اگر رباتِ جدید کورکورانه بفرستد، سفارش با هدفِ legacy ساخته می‌شود و باز «پول به سرویسِ اشتباه» می‌رود، این‌بار با ظاهرِ درست. راه‌حل: بیتِ توانایی در سشن — `per_account_renewal: bool = False` (فیلدِ response-model؛ بک‌اندِ جدید True، قدیمی غایب → False). ربات فقط وقتی ستونِ هدف را می‌فرستد که سشن True باشد؛ وگرنه همان رفتارِ فعلی (`renews_here` فقط روی یک کارت). همین الگو با `has_accounts` در راند ۱ آزموده شد.
- ترتیبِ دیپلویِ همان R5 می‌ماند: اول backend (بیتِ True در سشن)، بعد shopbot.

### ۲-۵. UX ربات برای «کدام سرویس؟»

**انتخاب‌گرِ جدا نمی‌سازیم** — خودِ کارت، انتخاب‌گر است (همان فلسفهٔ T3): هر کارتِ قابل-تمدید دکمهٔ «🔄 تمدید» خودش را می‌گیرد و کال‌بکش هدف را حمل می‌کند. صفحهٔ حجم برای مسیرِ `ren:` یک خطِ زمینه‌ای می‌گیرد: «تمدیدِ «گوشی خودم» — حجم تازه به همین سرویس اضافه می‌شود.» و صفحهٔ تأیید/پرداخت `target_label` را از intent نشان می‌دهد. مسیرِ «خرید» از منو (توسعهٔ آخرین سرویس) دست‌نخورده می‌ماند؛ «✨ سرویس جداگانه» هم (force_new) بدونِ هدف. برای مشتریِ تک-سرویسی صفر تپِ اضافه؛ برای چند-سرویسی از یک تپِ مبهم به یک تپِ صریح.

## ۳. ریسک‌های مسیرِ پول (و پاسخِ طراحی)

| ریسک | پاسخ |
|---|---|
| پول به سرویسِ اشتباه (همان باگی که این طراحی می‌کُشد) | هدف در سفارش **فریز** می‌شود؛ مالکیت در `/orders` و باز در `/orders/{id}/pay` چک می‌شود؛ گاردها per-TARGET؛ labelِ هدف روی هر دو صفحه |
| بک‌اندِ قدیمی فیلد را بی‌صدا می‌اندازد → هدفِ اشتباه با ظاهرِ درست | بیتِ `per_account_renewal` در سشن؛ ربات بدونِ بیت، هرگز فیلد را نمی‌فرستد (تستِ T12) |
| دو سفارشِ هم‌زمان روی یک حساب | مثل امروز: قفلِ per-user (`_lock_for`) مسیرِ ربات را سری‌ال می‌کند؛ تداخل با پنلِ اپراتور همان دفاعِ در-عمقِ `_extend_order` روی پنلِ زنده را دارد؛ تغییر نم‌دهیم |
| snapshot قیمت در برابر تغییر rate | نگاه‌داشته می‌شود — قیمت لحظهٔ سفارش روی `ShopOrder.price` فریز است؛ هدف فقط تعیین می‌کند «کدام حساب extended شود»، نه قیمت |
| تداخل با مسیرِ بدهی (`DEBT_LINE`) | بدهی پیش از هر چیز در `purchase()` چک می‌شود و پیامِ آن همان رلهٔ فعلی است — گاردِ بدهی per-customer می‌ماند (بدهیِ لجرِ مشتری است، نه حساب) |
| تمدیدِ حسابِ payg با بسته | همان `PAYG_TOPUP_INSTEAD` — حالا روی هدفِ واقعی، نه هدفِ ضمنی؛ پیامِ ربات سر جایش |

## ۴. فهرستِ تست (قرمز-اول، همان سبکِ هارنس‌های موجود)

1. **migration:** DB با شِمای قدیمی → init → ستون موجود و NULL؛ ردیف‌های قدیمی دست‌نخورده.
2. **پاریتی:** `resolve_renew_target(None)` == `renewable_account` روی همان فیکسچر (رفتارِ امروز بایت‌به‌بایت).
3. **مالکیت:** هدفِ shop_user دیگر → 404/ShopError بدون oracle؛ حسابِ مشتریِ وصل → مجاز.
4. **گارد per-target:** payg → `PAYG_TOPUP_INSTEAD`؛ cap=0 و cap=None → `SERVICE_IS_UNLIMITED`؛ disabled/deleted_from_marzban → رد.
5. **ترکیبِ ممنوع:** force_new + target → 400.
6. **خطِ پول:** `/orders/{id}/pay` با هدفِ دزدیده‌شده (مالکیت عوض‌شده بعد از ساخت سفارش) → 404 و صفر دبتِ کیف (harness مثل `test_shop_debt_pay`).
7. **فریزِ هدف:** ردیفِ ShopOrder بعد از ساخت، `target_account_id` درست دارد؛ `/quote` با هدف، `target_label` درست برمی‌گرداند.
8. **ربات:** کارت چند-سرویسی دکمهٔ `ren:<id>:<gb>` می‌سازد؛ `ren:` بدشکل → «قدیمی شده»؛ `renew:` قدیمی هنوز مسیرِ legacy؛ صفحهٔ حجم و تأیید، labelِ هدف را نشان می‌دهند.
9. **سازگاری:** سشنِ بدونِ `per_account_renewal` → payloadِ سفارش کلیدِ `target_account_id` **ندارد**؛ اضافه‌شدنِ فیلد به بک‌اندِ قدیمی نادیده‌خورده می‌شود (تستِ قراردادِ pydantic).
10. **سوییت‌های فعلی:** هر ۱۱ سوییتِ ربات + سوییت‌های `test_shop*` بدون رگرسیون؛ و اگر روزی پیاده شد، سناریوی «چند-سرویسی تمدیدِ کارتِ قدیمی‌تر» به `test_walkthrough_9_journeys` اضافه شود.

## ۵. ترتیبِ پیاده‌سازی پیشنهادی (برای راندِ بعد، با GO مالک)

1. migration + هلپرِ `visible_account_ids`/`resolve_renew_target` + تست‌های ۱–۵ (هیچ رباتی هنوز فرستنده نیست).
2. endpointها + بیتِ سشن + تست‌های ۶–۷ و ۹.
3. ربات (کال‌بک/UX) + تست‌های ۸ + رگرسیونِ کامل + آپدیتِ ترنسکریپت (`dump_screens.py`).
4. یک ریلیز بعد: حذفِ `renews_here` (ربات و بک‌اند با هم).

</div>
