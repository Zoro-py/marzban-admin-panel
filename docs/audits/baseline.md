# Baseline — fix/vpn-completion-2026-09 (2026-09-20)

## روش اجرا

نکتهٔ مهم محیطی: **هیچ‌کدام از ۱۶ فایل تست backend و ۲ فایل تست bot از pytest استفاده نمی‌کنند** —
همه اسکریپت مستقل‌اند و با `python -m tests.<name>` (از `backend/`) و `python -m test_<name>` (از `bot/`)
اجرا می‌شوند. اجرای آن‌ها زیر pytest با `INTERNALERROR` (به‌خاطر `sys.exit(1)` در سطح ماژول) می‌شکند؛
این شکستِ pytest **باگ پروژه نیست** — docstring خود تست‌ها صریح می‌گوید «no pytest». برای baseline،
هر ۱۸ فایل به شکل رسمی خودشان اجرا شد (exit code = معیار).

## backend — ۱۶/۱۶ پاس (venv: `backend/venv`, Python 3.14, pytest هم نصب شد ولی ملاک نیست)

| تست | exit |
|---|---|
| tests.test_account_delete | 0 |
| tests.test_balance_gb | 0 |
| tests.test_balance_since | 0 |
| tests.test_bot_phone | 0 |
| tests.test_bulk_accounts | 0 |
| tests.test_created_by | 0 |
| tests.test_debt_nudge_endpoint | 0 |
| tests.test_delegate_smoke | 0 |
| tests.test_lifetime_traffic_backfill | 0 |
| tests.test_lifetime_traffic_rebaseline | 0 |
| tests.test_monthly_avg | 0 |
| tests.test_payg_shape | 0 |
| tests.test_rate_history | 0 |
| tests.test_receipt_alert | 0 |
| tests.test_receipt_text_dedupe | 0 |
| tests.test_shop | 0 |

## bot — ۲/۲ پاس

| تست | exit |
|---|---|
| bot.test_debt_console | 0 |
| bot.test_toolbox | 0 |

## compile / build

- `python -m py_compile` روی `backend/app` + `backend/tests` → تمیز
- `python -m py_compile` روی `bot/` + `shopbot/` + `delegate_bot/` → تمیز
- `npm run build` در `frontend/` → ✓ built (651ms)، بدون خطا

## نکتهٔ CI

`.github/workflows/ci.yml` فقط import-check می‌کند و هیچ تستی را اجرا نمی‌کند (فاز ۱ بررسی می‌شود — بند C10).
