# دور ممیزی ۲۰۲۶-۰۹-۲۹ — فاز ۳: سوییپ کامل Playwright (U1-MOBILE)

> اجرا با `scripts/panel_harness.py` (بک‌اند واقعی + فرانت بیلدشده روی **کپی** DB زنده، بدون scheduler، مرزبان فیک) + `scripts/panel_sweep.mjs` (Playwright/Chromium).
> **نتیجه: تمام سبز — صفر خطای console/pageerror در کل ماتریس، همهٔ پروب‌ها پاس.** خروجی خام: `temp/sweep_0929/` (۶۵ اسکرین‌شات + `report.json`)؛ ۹ نمای کلیدی در همین پوشه کپی شده.

## ماتریس

- **۱۳ صفحه** (Dashboard, Accounts, AccountInspector ‏?acct=1, Customers, CustomerDetail, Groups, GroupDetail, Finance, History, MonthlySettlements, Delegates, Servers, Login)
- × ‏**۳۹۰px موبایل + ‏1280px دسکتاپ** × ‏**light + dark** + یک نمای FA برای هر صفحه = **۶۵ اسکرین‌شات**
- جمع‌آوری console error / pageerror / پاسخ‌های 4xx+ در هر بارگذاری

## پروب‌های تابعی (همه پاس)

| پروب | نتیجه |
|---|---|
| R3 — سورت هر ستون جدول اکانت‌ها | ۷ هدر Sortable کلیک شد، بدون خطا |
| Date-picker سراسری | پریست «Jalali month» نوشت `?since=2026-09-23` در URL و هدر جدول به «Owed since» عوض شد |
| سوییچ auto-renew ردیفی (بند ۲.۷) | ‏۱۴۵ سوییچ در جدول دسکتاپ رندر شد؛ دکمه‌های batch ‏«Auto-renew off/on» در نوار انتخاب حاضرند |
| R9 — Copy invoice گروه = بدهی لحظه‌ای | کلیپ‌بورد بعد از نرمال‌سازی ارقام فارسی (و جداکنندهٔ هزارگان ‏U+066C): ‏**جمع کل = ‏۱٬۵۰۰٬۹۵۳ == ‏API ‏net_owed ‏۱٬۵۰۰٬۹۵۳٫۳۸** (گردشده) — ردیف‌به‌ردیف اعضا هم با API یکی |
| ChargeHistoryPreview سه mount | AccountInspector (بخش History باز شد)، GroupDetail، CustomerDetail — هر سه «Open full history» دارند |
| نمودارها | History ‏۲۳ نود، Dashboard ‏۲۶ نود، Finance رندر — echarts با **SVG renderer** (پروب اول فقط canvas می‌شمرد و فالس‌نگتیو بود؛ اصلاح شد) |
| Finance | ‏`charged_this_month=16,023,130.82` با داشبورد هم‌عدد |
| Settings dialog + صفحهٔ Servers | باز/بارگذاری شد |
| دسترس‌پذیری (کیبورد) | Tab-walk روی /accounts و / به‌ترتیب ۴۰ و ۲۰ توقف، به body برمی‌گردد — بن‌بست کیبوردی ندارد |
| i18n فارسی | با `lang=fa` صفحهٔ History تیتر «تاریخچه» و متن فارسی رندر می‌کند |

## دو اصلاح ریز که همین سوییپ پیدا کرد

1. **ستون Auto-renew در ۳۹۰px** رندر اولیه OWES NOW را بیرون می‌زد (ضد تضمین موجود «account/usage/owes در هر عرض») → ستون زیر ‏`sm` مخفی شد (بَج خاموشی + batch + inspector در موبایل کفایت می‌کند)؛ نمای موبایل بعد از اصلاح: سه ستون کلیدی سر جایشان.
2. خیر — مورد دوم یافتهٔ ابزار بود نه کد (پروب‌های canvas/SVG و ارقام فارسی؛ ثبت برای شفافیت، تغییر کد نداشتند).

## نکتهٔ 404 بعد-plan

GET ‏`/api/accounts/{id}/next-plan` برای اکانت بدون پلن عمداً 404 برمی‌گرداند و فرانت آن را می‌گیرد (null رندر می‌کند) — مرورگر خط 404 را در console می‌نویسد؛ این نویزِ ذاتی قرارداد است نه باگ، در سوییپ correlate و مستثنا شد.
