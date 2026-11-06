# Z Code progress — Shop bot UX rebuild + panel tasks

<div dir="rtl">

مسترپرامپت: `docs/ZCODE_SHOPBOT_UX_PROMPT.md` · شاخه: `feat/shopbot-ux-rebuild` (از بیس `2b57db2` روی `feat/admin-ux-rebuild-2026-10-24`)
قانون: هر تسک = کد + تست + سند + یک کامیت `shopbot-ux: T<n> …`. هیچ پوش/دیپلوی/SSH به france-vpn.

## T0 — ممیزی پایه (read-only) — ✅ انجام شد

- **تغییر فایل‌ها:** `docs/ZCODE_SHOPBOT_UX_AUDIT.md` (جدید، کل خروجی تسک) — بدون هیچ تغییر کد.
- **خوانده شد در کامل:** `shopbot/handlers/shop.py` (۱۴۹۱)، `shopbot/texts.py` (۷۶۱)، `shopbot/bot.py`، `shopbot/api_client.py`، `backend/app/shop_texts.py` (۴۲۵) + grepهای وریفای در `backend/app/shop_service.py`.
- **تست‌های اجراشده (بیس‌لاین، همه سبز):**
  - `../bot/venv/Scripts/python.exe test_prepublic_p0p1_bot.py` → All passed
  - `../bot/venv/Scripts/python.exe test_shop_link_claim.py` → All passed
  - `../bot/venv/Scripts/python.exe test_texts_are_strings.py` → All passed
- **یافته‌های کلیدی:** منوی بدترین حالت = ۷ ردیف/۱۰ دکمه؛ فقط «خرید با کیف‌پول» به هدف ≤۳ تپ می‌رسد (خریدِ مشتریِ جدید = ۴ تپ، بدهی با رسید = ۴ تپ)؛ سه درِ جدا برای «حساب من»؛ ۱۰ بن‌بست/تله ثبت شد (مهم‌ترین: کیبوردِ پرامپت شماره منو را می‌بلعد؛ دکمهٔ تمدیدِ کهنه سفارشِ قدیمی می‌سازد؛ سؤال حین انتظارِ رسید جواب نمی‌گیرد).
- **یافتهٔ فنیِ خارج از محدودهٔ UX (برای Part B-3):** شاخهٔ `_STATE_AWAITING_OPTIONAL_NAME` (shop.py:1372–1392) مرده است و به `_create_order_and_ask_payment` صدا می‌زند که تعریف نشده (NameError نهفته)؛ متنِ `ASK_OPTIONAL_NAME` یتیم. باگِ چسبیدنِ خط در `ASK_OPTIONAL_USERNAME` (texts.py:563–567، `\n` جاافتاده) — این یکی داخل محدودهٔ T6 فیکس می‌شود.
- **سؤال باز از مالک:** هنوز هیچ (T1 ممکن است سؤال منو تولید کند).

</div>
