# Z Code prompt — Telegram Mini App for the shop bot («BotFather-style» UI)

You work in `C:\Users\Laptop Markazi\Desktop\work\VPN`. Create branch `feat/shop-miniapp` from
`feat/shopbot-ux-rebuild` (already deployed to production on 2026-11-06; HEAD `6feec32`). Do the tasks **in order**,
one at a time; finish each (code + tests + evidence in `docs/ZCODE_MINIAPP_PROGRESS.md` + one local commit
`miniapp: M<n> ...`) before the next.

## 0. What the owner wants
The shop bot's chat UX was rebuilt (menu of 4 doors, «حساب من», service cards). On top of it the owner wants a
**Telegram Mini App** opened from the bot (menu button + inline button), looking and feeling like the BotFather
Mini App: a sheet that slides over the chat with a header (shop name), a search field, big tappable rows, and
sections (like «My bots» → «My services»), a prominent primary row («Create a New Bot» → «خرید سرویس جدید»).
Principle unchanged: **one balance** (usage makes it negative, top-up positive), minimum taps, calm Persian copy.
Customers are ordinary Iranian VPN buyers on phones, many on slow/filtered networks.

## 1. Mandatory reading (absolute paths; you cannot invoke skills, read them; STOP if one cannot be read)
- `C:\Users\Laptop Markazi\Desktop\work\Shiraze\cursor\.claude\skills\ux-engineered-design\SKILL.md` (screen = a Mini App view; apply the 20-hat review + persona walkthroughs)
- `C:\Users\Laptop Markazi\Desktop\work\Shiraze\cursor\.claude\skills\persian-doctor-copy\SKILL.md` (principles only)
- `C:\Users\Laptop Markazi\Desktop\work\Shiraze\cursor\.claude\skills\tdd\SKILL.md`
- `C:\Users\Laptop Markazi\Desktop\work\Shiraze\cursor\.claude\skills\diagnosing-bugs\SKILL.md`
- memory notes in `C:\Users\Laptop Markazi\.claude\projects\c--Users-Laptop-Markazi-Desktop-work-Shiraze-cursor\memory\`:
  `feedback_no_system_burden_in_user_copy.md`, `feedback_rtl_output.md`, `feedback_ux_review_must_be_clickable_2026_10_22.md`,
  `project_vpn_reseller_ledger_seyed_family_2026_11_03.md`.
- Repo docs you must know before designing: `docs/ZCODE_SHOPBOT_UX_DESIGN.md`, `docs/SHOPBOT_SCREENS.md`,
  `docs/ZCODE_SHOPBOT_REVIEW.md`, `docs/ZCODE_SHOPBOT_RENEW_TARGET_DESIGN.md`, `AGENTS.md`.
- Telegram Mini Apps official docs (fetch if you have web access, else rely on knowledge): `Telegram.WebApp`
  API: `initData`, `initDataUnsafe`, `themeParams`, `MainButton`, `BackButton`, `HapticFeedback`, `openLink`,
  `openTelegramLink`, `close`, `expand`, `ready`, safe-area/viewport events, and server-side initData validation.

## 2. Hard rules
- **No deploy, no push, no SSH, no production access, no real customer data.** Local branch + commits only.
  The owner's assistant deploys; DNS/BotFather/domain steps are the owner's (list them in a runbook, do not do them).
- **Do not change money math** (`backend/app/services.py`, `statement.py`, ledger, wallet debit/credit, purchase /
  renew / pay logic in `shop_service.py`). The Mini App only *calls the same existing logic*. A needed money-path
  change → write it under "OWNER/CLAUDE MUST DECIDE" with file:line and stop that item.
- The Mini App is **separate from the admin panel**: new folder `miniapp/` with its own build and its own nginx
  container (add a service to `docker-compose.yml`, not wired to production). It never contains admin credentials,
  the bot token, or `SHOP_BOT_API_KEY`. Never reuse the admin frontend or admin auth.
- Auth = Telegram `initData` only, validated **server-side** (see M1). Never trust `initDataUnsafe`, never accept a
  `telegram_id` from the request body for authorization.
- Tests use throw-away data only (`zz_*`, temp sqlite). `PYTHONUTF8=1`. Preserve line endings (files may be CRLF;
  never put `\n` inside a python heredoc string literal when patching). No `/tmp`.
- Persian: RTL (`dir="rtl"`), Persian digits for display, money via the same format as the bot
  (`شمار + «تومان»`, thousands separator ٬), no raw Latin URL inside Persian sentences (use buttons). Fonts must be
  self-hosted (no Google Fonts; filtered networks). No external CDN, no analytics, no tracking.
- Keep the bundle small (target < 150 KB gzipped, no heavy framework needed — vanilla TS or Preact is fine; pick one
  and justify in one line). Must work in Telegram Desktop, Android and iOS WebViews.

## 3. Tasks

### M0. Inventory + design (no code)
Write `docs/ZCODE_MINIAPP_DESIGN.md`: (a) the list of Mini App screens, mapped 1:1 to bot flows it replaces or
complements; (b) the BotFather reference translated to ours — header (shop name + support), search (filters services
by name/size), section «سرویس‌های من» with one row per service (name/size, remaining GB bar, days left, status
dot), primary row «خرید سرویس جدید», row «حساب من» (ONE number: موجودی or ماندهٔ پرداخت‌نشده), row «راهنما و
پشتیبانی»; (c) 2 alternative layouts as text mockups, choose one with reasons; (d) tap targets: buy ≤ 3 taps, renew
≤ 2, pay debt ≤ 2, see usage 0; (e) states: loading skeleton, empty, offline/slow network, expired session,
blocked user, shop closed; (f) what stays in the chat (receipt photo upload is v1 = deep link back to the bot chat with
a pre-filled start payload; Mini App never handles card numbers beyond showing them with a copy button).
(g) Theme: use `Telegram.WebApp.themeParams` CSS variables so dark/light match the user's Telegram.

### M1. Backend: initData auth + read-only endpoints (TDD)
Add `backend/app/routers/shop_webapp.py` (router prefix `/api/shop/webapp`), registered like the existing shop routers:
1. `validate_init_data(init_data: str, bot_token: str, max_age_s=3600) -> dict`: parse query string, remove `hash`,
   build data-check-string (sorted `key=value` joined by `\n`), `secret = HMAC_SHA256(key=b"WebAppData", msg=bot_token)`,
   compare `HMAC_SHA256(secret, dcs)` hex with **constant-time** compare; reject if `auth_date` older than max age or in
   the future (>60 s skew); return the parsed `user` JSON. Unit tests with a known-good vector built in the test
   itself plus tampered hash, tampered field, expired, missing hash, malformed user, replayed-with-other-user.
2. FastAPI dependency `webapp_user` reading header `X-Telegram-Init-Data`; resolves the `ShopUser` by
   `user.id` (never creates money state; creating the ShopUser row is allowed only through the existing
   `/session` path — reuse it). 401 on any validation failure, no oracle in the message.
3. Read-only endpoints that **reuse** the existing service functions (do not duplicate logic): `GET /me` (same data
   as bot session: balance/debt/exact flag, has_accounts, shop name, support handle, is_open/is_blocked),
   `GET /accounts` (same rows as `bot_list_accounts`, including `renews_here`, with usage %/days computed
   server-side), `GET /plans` (min/max GB, price per GB, duration, payg flag), `POST /quote` (reuse the existing
   quote logic; read-only).
4. Rate limit per Telegram user (reuse existing limiter if there is one), CORS restricted to the Mini App origin
   from a new env `MINIAPP_ORIGIN`, `Cache-Control: no-store`.
Tests (backend test style, `python -m tests.test_shop_webapp` from `backend/`): auth matrix above; user A can never
read user B's data even with a forged `telegram_id` query/body; blocked user; unknown user; every endpoint returns
the same numbers as the bot endpoints for the same user (parity test).

### M2. Backend: action endpoints (money-adjacent — wrapper only)
`POST /orders` (create), `POST /orders/{id}/pay` (wallet pay), `POST /pay-debt-from-wallet`, `POST /trial` — thin wrappers
that call the **same** shop_service functions the bot endpoints call, with the user from `webapp_user`. Idempotency:
require an `Idempotency-Key` header, store (user, key) → response for 10 minutes, so a double-tap or WebView retry
cannot charge twice (reuse the existing double-spend guards; the test must prove one debit for two identical requests).
Tests: double submit, stale order, someone else's order id, insufficient balance, shop closed, blocked user,
parity of resulting ledger rows with the bot path. If any wrapper would need a change inside money functions →
stop and log it.

### M3. The Mini App UI (`miniapp/`)
Build the chosen design: home (header, search, «سرویس‌های من» rows, primary «خرید سرویس جدید» row, «حساب من» row,
help row), service detail sheet (usage bar, days, status; buttons: **تمدید** only where `renews_here`, else a calm
line «برای تمدید این سرویس با پشتیبانی هماهنگ کنید» — per-account renewal is NOT built yet, see
`ZCODE_SHOPBOT_RENEW_TARGET_DESIGN.md`; **لینک اشتراک** via `Telegram.WebApp.openLink`/copy; QR shown inline), buy flow
(volume chips + custom → one summary → one confirm via Telegram `MainButton`; wallet-covers = pay in the same tap;
otherwise show card number with copy buttons for the number and the amount and a «ارسال رسید در گفتگو» button that
closes the Mini App and opens the bot chat with the receipt flow), «حساب من» (one number, primary action pay/top-up,
details on tap), skeleton loaders, error and offline states with a retry button, `BackButton` wired to navigation,
haptics on success/error. Small, accessible (min 44 px targets, contrast in both themes), animations ≤ 200 ms and
respecting `prefers-reduced-motion`. All copy in one `copy.fa.ts` file following the bot's single vocabulary
(موجودی / ماندهٔ پرداخت‌نشده / شارژ / تمدید). Dev mode: a mock `Telegram.WebApp` + mock API so the owner can click
through it in a browser (`npm run dev:mock`) — **clickable review is mandatory** (ux skill).

### M4. Bot integration (shopbot/, no behaviour removed)
Add (behind env `MINIAPP_URL`; if unset nothing changes): an inline `WebAppInfo` button «🧩 باز کردن در برنامه» on
the main-menu message and on the «حساب من» screen, and a documented helper `scripts/set_miniapp_menu_button.py` that
calls `setChatMenuButton` (type `web_app`, text «فروشگاه») — **not run**, owner runs it. Deep-link payloads
`?start=receipt_<n>` back into the chat. All existing handlers/callbacks keep working. Bot tests stay green; add tests
for the new button presence/absence by env.

### M5. Verification
- Backend: all new suites + the existing ones (`test_shop`, `test_shop_debt_pay`, `test_shop_sync_mark`,
  `test_shop_payg_link_edges`, `test_receipt_alert`, `test_shop_delivery_one_message`, `test_statement`) green.
- Bot: all `shopbot/test_*.py` green.
- Mini App: unit tests for formatting (Persian digits/money), api client (initData header, retries, idempotency
  key), and a headless-browser run (Playwright) of the 6 journeys against the mock API in a 360×640 viewport, dark
  and light theme, RTL; save screenshots to `docs/miniapp_shots/`.
- Run the 20-hat review and 4 persona walkthroughs (55-year-old on a cheap phone; first-timer who wants only the trial;
  annoyed debtor; reseller family member buying for 3); log findings and fix or list them.
- Security checklist written in the progress file: initData validation, replay window, per-user scoping, CORS,
  rate limit, CSP header for the Mini App (`default-src 'self'; script-src 'self' https://telegram.org`; `frame-ancestors`
  limited to Telegram web origins), no secrets in the bundle (grep the build for the token/key patterns), dependencies
  pinned.

### M6. Deploy runbook (not applied)
`docs/deploys/2026-11-06_miniapp.md`: owner steps (they need me or the owner, mark clearly): a subdomain + HTTPS
(Cloudflare/NPM — look at how the existing portal/gateway is exposed in `docs/` and mirror it), new env vars
(`MINIAPP_URL`, `MINIAPP_ORIGIN`), BotFather steps (`/newapp` or the bot's menu button via
`setChatMenuButton`, domain set with `/setdomain` if needed — Telegram requires HTTPS and a stable domain),
order of deploy (backend → miniapp container → shopbot), sqlite `.backup` pre-flight, smoke checklist on a throw-away
account, rollback (env unset = feature off; previous image).

## 4. NOT yours
Domain/DNS/SSL, BotFather clicks, real tokens, production deploy, any money-math change, per-account renewal backend
(M-1), the owner's open ledger decisions (real_seyed_tun, Seyed payments, mahan, Azno, US tunnel).

## 5. Final report
Per task: status, exact commands + results, commit hashes, screenshots path, security checklist result, questions for
the owner, and the "OWNER/CLAUDE MUST DECIDE" list. Do not claim anything you did not run.
