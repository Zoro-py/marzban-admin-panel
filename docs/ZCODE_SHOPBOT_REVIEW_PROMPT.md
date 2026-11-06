# Z Code prompt — Round 2: review, screen transcript, deploy sheet (shop bot UX)

You are in `C:\Users\Laptop Markazi\Desktop\work\VPN`, branch `feat/shopbot-ux-rebuild`
(HEAD includes `0aa626d`). Round 1 rebuilt the shop bot UX (T0–T8, see `docs/ZCODE_PROGRESS.md`).
Round 1's tests are green, but nobody has READ the result critically or SEEN it as the customer will.
That is this round. Do the tasks **in order**, one at a time; finish each (evidence + local commit
`shopbot-ux-r2: R<n> ...` + entry in `docs/ZCODE_PROGRESS.md` under "Round 2") before the next.

## 0. Read first (mandatory; if a file cannot be read, STOP and say so)
Same skills as round 1, by absolute path (you cannot invoke them, read them):
- `C:\Users\Laptop Markazi\Desktop\work\Shiraze\cursor\.claude\skills\ux-engineered-design\SKILL.md`
- `C:\Users\Laptop Markazi\Desktop\work\Shiraze\cursor\.claude\skills\persian-doctor-copy\SKILL.md` (principles only; our readers are VPN customers)
- `C:\Users\Laptop Markazi\Desktop\work\Shiraze\cursor\.claude\skills\diagnosing-bugs\SKILL.md`
- `C:\Users\Laptop Markazi\Desktop\work\Shiraze\cursor\.claude\skills\tdd\SKILL.md`
- memory notes in `C:\Users\Laptop Markazi\.claude\projects\c--Users-Laptop-Markazi-Desktop-work-Shiraze-cursor\memory\`:
  `feedback_no_system_burden_in_user_copy.md`, `feedback_rtl_output.md`,
  `feedback_doctor_copy_conditions_as_information_2026_10_26.md`, `project_vpn_reseller_ledger_seyed_family_2026_11_03.md`.

## 1. Hard rules (unchanged)
No deploy, no push, no SSH, no production access, no real customer data. Local commits only.
Do NOT change money math (`backend/app/services.py`, `statement.py`, ledger, wallet debit/credit,
`shop_service` purchase/renew/pay logic). If a review finding touches money, **do not fix it**: write it
under "OWNER/CLAUDE MUST DECIDE" in the progress file with file:line and a failing-case description.
`PYTHONUTF8=1`; keep line endings (files may be CRLF — edit through a tool that preserves them,
never `\n` inside a python heredoc string literal); absolute paths; no `/tmp`.
Persian output must be RTL-safe: no raw Latin URL inside Persian sentences.

## R1. Critical code review of the round-1 diff (read-only first)
Run `git diff 2b57db2..HEAD -- shopbot/ backend/app` and read **every changed line of
`shopbot/handlers/shop.py` (≈750 lines) and `shopbot/bot.py`**. Hunt for real bugs, not style:
1. **State leaks:** `context.user_data` keys set in one flow and still present when another starts
   (buy → account → receipt → cancel). Walk each ConversationHandler/state; every exit path must clear state.
2. **Handler order/regex collisions** in `bot.py`: for each callback pattern and each text filter,
   list which handler wins; find any pattern that shadows another (e.g. `^menu:`, `^debt:`, `^acct:`, `vol:`).
3. **Old inline buttons in users' chats:** list every callback_data the OLD code could emit
   (`git show 2b57db2:shopbot/handlers/shop.py` and `git show 2b57db2:shopbot/bot.py`) and prove with a test
   that each is still routed and never crashes or silently does nothing.
4. **One-tap money buttons** (wallet confirm, "debt pay from balance", renew): trace each to its backend
   call. Check: double-tap, stale button after balance changed, button pressed by a different Telegram user,
   amount shown on the button vs amount actually sent. Write failing tests for any gap you can demonstrate.
   (Report money-path defects; do not edit backend money code.)
5. **Failure paths:** backend 4xx/5xx/timeouts during each step: does the customer get a clear message and a
   way back, or a stack trace/silence?
6. **Session fields:** the bot reads `has_accounts` / `has_payg_service` / `renews_here`. What happens if
   they are missing (old backend) or the session call fails? Prove with tests.
Output: a findings table in `docs/ZCODE_SHOPBOT_REVIEW.md` (id, severity, file:line, scenario, status).
Fix non-money bugs with test-first commits; log the rest.

## R2. Persian copy read-through (as a native speaker would)
Read all of `shopbot/texts.py` and every inline button label in `handlers/shop.py` out loud, mentally.
Check each text for: grammar and register consistency (one register, "-تان"), awkward or machine-sounding
phrasing, wrong or missing ZWNJ (نیم‌فاصله) in compounds (می‌خواهید، سرویس‌ها، تمدیدتان), mixed digits
(all Persian digits via `texts.fa()` where shown to customers), Latin leftovers, tone (information not
orders, no blame, calm about money), any promise the bot cannot keep, and consistency with the single
vocabulary (موجودی / ماندهٔ پرداخت‌نشده / شارژ / تمدید). Check button labels fit one line on a narrow phone
(≤ ~22 chars) and that emoji do not break RTL ordering.
Output: table old → proposed → why for **every** text you would change, in
`docs/ZCODE_SHOPBOT_COPY_REVIEW.md`. Apply only changes that are clearly better; keep the rest as
proposals for the owner. Keep `test_texts_are_strings.py` and all bot tests green.

## R3. Screen transcript — so the owner can SEE the bot without Telegram
Write `shopbot/dump_screens.py` (stubbed Telegram + backend, same style as the tests) that drives each
journey and writes `docs/SHOPBOT_SCREENS.md`: for every step print the bot's message text exactly
(in a fenced block) and its buttons as a small table (label → callback/URL). Cover, for **each of four
customer states** — brand-new, has 1 active service, has 2 services (one ended), owes money (and a
separate case with positive balance):
main menu; my services; renew tap; buy (volume screen → summary → confirm; wallet-covers and
card-to-card variants); account screen + details; pay debt (wallet/receipt/cancel); top-up; trial;
help/support; free text that matches nothing; a stale button. Mark every screen with its tap count.
Then re-read the transcript yourself as the customer and list in `docs/ZCODE_SHOPBOT_REVIEW.md` anything
confusing, redundant, or longer than 6 lines; fix what is clearly wrong.

## R4. Weekly-usage script: run it for real on fake data and check the logic
`scripts/weekly_usage_comparison_2026-11-06.py` was only smoke-tested with fakes. Build a **temp sqlite**
(never the production file) with 3 accounts across 5 weeks of usage including: a Marzban reset mid-period,
an account on inbound 6005 and one on FR 6009 (ضریب ۲), an account created inside the window, a deleted
account. Compute the expected weekly numbers BY HAND first (write them in a comment), then run the script
and compare. Confirm it never writes (open the DB read-only / assert row counts unchanged). Fix any
discrepancy; keep it read-only. Do not run it against production.

## R5. Compatibility + deploy sheet
Backend and bot deploy separately (backend `docker compose up -d --build backend` on france-vpn; bot is its own
service). Work out and test: new bot + OLD backend (fields missing), OLD bot + new backend (extra fields
ignored; delivery message is now one photo with caption + URL button — does the old bot's flow break?).
Write `docs/deploys/2026-11-06_shopbot_ux.md`: exact order, pre-flight (sqlite `.backup` to
`/app/data/manual_backups/`), commands, post-deploy smoke checklist using a throw-away test account (never a
real customer), and the rollback (previous image / `git revert` range). Mark it "NOT APPLIED — owner GO required".

## R6. Design only (no code): per-account renewal
Today the renew purchase path extends `shop_service.renewable_account` (the newest delivered, non-disabled
account) — there is no account id in the order. Round 1 + `0aa626d` hide the renew button on all other cards of a
multi-service customer (backend field `renews_here`). Write `docs/ZCODE_SHOPBOT_RENEW_TARGET_DESIGN.md`:
what a real per-account renewal needs (ShopOrder column + migration style used in this repo, order/quote/pay
endpoints, ownership check incl. linked customers, `refuse_unlimited_renewal`/payg guards per target, callback
payload, bot UX for choosing which service), the risks to money paths, and a test list. Do not implement.

## Final report
Per task: status, commands + real output summary, commit hashes, findings by severity, items under
"OWNER/CLAUDE MUST DECIDE". Do not claim anything you did not run.
