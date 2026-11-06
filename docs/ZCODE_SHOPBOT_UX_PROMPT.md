# Z Code master prompt — Shop bot UX rebuild + remaining panel tasks

You are working in the VPN reseller panel repo `C:\Users\Laptop Markazi\Desktop\work\VPN`
(Telegram shop bot in `shopbot/`, backend in `backend/`). Do the tasks below **one at a time,
in order**. Finish a task completely (code, tests, evidence, local commit) before starting the next.

## 0. Why this exists
The owner's verdict on the shop bot: "UX is beyond awful — too many clicks, confusing texts,
too much complexity." The owner's product principle: **one balance. Usage makes it negative, a
top-up makes it positive.** Customers are ordinary Iranian VPN buyers on a phone, often in a hurry,
not technical. Every screen must answer "what do I do now?" in one glance.

## 1. Use the installed skills — mandatory, before touching anything
Read these files in full first (absolute paths, they are Claude Code skills; you cannot invoke them,
so read and follow them as written):

1. `C:\Users\Laptop Markazi\Desktop\work\Shiraze\cursor\.claude\skills\ux-engineered-design\SKILL.md`
   — the UX pipeline: frame the user's job first (psychology before screens), 20-hat review,
   persona walkthroughs, build disputed options as switchable variants, owner reviews LIVE.
   Apply it to the bot: "screen" = a Telegram message + keyboard.
2. `C:\Users\Laptop Markazi\Desktop\work\Shiraze\cursor\.claude\skills\persian-doctor-copy\SKILL.md`
   — the Persian wording rules (feelings the text must not cause, grammar, word list). Its readers are
   doctors; ours are VPN customers, so reuse the *principles* (information not orders, no blame, no
   system burden, short, one verb per action) and ignore doctor/P24-specific parts.
3. `C:\Users\Laptop Markazi\Desktop\work\Shiraze\cursor\.claude\skills\tdd\SKILL.md` — test-first for logic changes.
4. `C:\Users\Laptop Markazi\Desktop\work\Shiraze\cursor\.claude\skills\diagnosing-bugs\SKILL.md` — for the failing tests in Part B.
5. Also read the owner's memory notes (plain markdown) in
   `C:\Users\Laptop Markazi\.claude\projects\c--Users-Laptop-Markazi-Desktop-work-Shiraze-cursor\memory\`:
   `feedback_no_system_burden_in_user_copy.md`, `feedback_rtl_output.md`,
   `feedback_doctor_copy_conditions_as_information_2026_10_26.md`,
   `feedback_release_announce_in_product_2026_10_26.md`,
   `project_vpn_reseller_ledger_seyed_family_2026_11_03.md`.

If a skill file cannot be read, STOP and say so; do not improvise the method.

## 2. Hard rules
- **No deploy, no push, no SSH to france-vpn, no production DB writes.** Local branch + local commits
  only: create `feat/shopbot-ux-rebuild` from the current HEAD. The owner's assistant deploys.
- Never test on a real customer account. Tests use throw-away data only (`zz_*` names, temp sqlite).
- Persian output: RTL-safe. Never put a raw Latin URL inside a Persian sentence (it gets bidi-scrambled;
  use an inline URL button). Persian digits via `texts.fa()`; money via `texts.money()`.
- Do not change money math (`backend/app/services.py`, `statement.py`, ledger). UX/copy/flow only,
  unless a task below says otherwise. Ledger is append-only.
- Keep every existing callback pattern working until its replacement ships in the same commit
  (old inline buttons may still sit in users' chats).
- Windows: set `PYTHONUTF8=1`; preserve existing line endings; no `/tmp`.
- After each task: update `docs/ZCODE_PROGRESS.md` (task, files changed, tests run + output,
  before/after click counts, open questions) and make one local commit
  `shopbot-ux: T<n> <summary>`. Then continue to the next task. If blocked by an owner decision,
  write the question into the progress file and skip to the next task.

## 3. Part A — Shop bot UX (do in this order)

### T0. Baseline audit (read-only)
Map every journey as a numbered tap/typing sequence from the real code
(`shopbot/handlers/shop.py`, `shopbot/texts.py`, `shopbot/bot.py`) and count taps + messages:
(1) first visit → free trial, (2) buy a service (new customer, card-to-card), (3) buy with wallet,
(4) renew an existing service, (5) top up wallet, (6) pay invoice debt, (7) check usage/remaining days,
(8) get the subscription link again, (9) contact support / get help.
Also list every menu button, every dead end, every place free text is not understood, and every
message longer than ~6 lines. Write it as a table in `docs/ZCODE_SHOPBOT_UX_AUDIT.md`
(columns: journey, current taps, current messages, pain). Start from facts already seen:
the main menu can show 7-8 rows (trial, buy, my services, wallet, top-up, pay debt, "new separate
service", pay-as-you-go, help, support); wallet/top-up/debt are three separate entries for one idea.

### T1. Define the target experience (design, no code yet)
Using the ux-engineered-design framing, write for each journey: the user's job, their fear, the minimum
possible taps, and the new screen sequence. Targets (hard goals): buy = **≤3 taps** after the menu;
renew = **1 tap** from the service card; pay debt = **≤2 taps**; see usage = **0 taps** (the
"my services" card already shows it); every screen has exactly **one obvious primary action**.
Propose a main menu of **at most 4-5 buttons**, contextual to the customer (new / has services /
owes money). Produce 2-3 alternative menu layouts as text mockups and pick one with reasons, in
`docs/ZCODE_SHOPBOT_UX_DESIGN.md`. Stop here only if two options are truly 50/50; otherwise decide.

### T2. Main menu + navigation
Implement the chosen menu. Merge wallet / top-up / pay-debt into one **"حساب من"** entry that shows
ONE number (balance or debt, never both side by side) with the right primary button
("پرداخت" when negative, "شارژ" when positive). Move "new separate service", pay-as-you-go,
help and support out of the top level (inside buy flow / a single "راهنما و پشتیبانی" entry).
Every sub-screen has a visible way back to the menu; `/start` always resets state.

### T3. "My services" card
One message per service, inline buttons on the card itself: **تمدید** (1 tap, uses the existing
renew path), **لینک اشتراک** (URL button), **⋯ بیشتر** (name, auto-renew). Show: name or size,
remaining GB as a bar + number, remaining days, status in plain words. When expired/limited, the
primary button is renew and the sentence says what happened and what one tap does.

### T4. Buy flow
Volume choice as inline buttons (a few popular sizes + "حجم دلخواه"), then ONE summary message
(size, days, price) with ONE confirm button. If wallet covers it, pay from wallet in that same tap;
otherwise show card number with a copy-friendly format and the single instruction "رسید را بفرستید".
No duplicate explanations; send the service (link button + QR) in one message when ready.

### T5. Debt and wallet language (one balance)
Rewrite `debt_status`, `wallet_summary`, `debt_*`, `DEBT_NOT_EXACT`, `STATUS_SUSPENDED_BADGE` etc. so a
customer reads: "موجودی شما: X" or "مانده‌ی پرداخت‌نشده: X", one sentence on what to do. The per-account
breakdown (`debt_breakdown`) appears only after tapping "جزئیات", never by default. Keep the
"approximate" notice but shorten it and make it calm (information, not alarm). No words like
ledger/wallet/payg/invoice-jargon the customer wouldn't use; pick one vocabulary and use it everywhere.

### T6. Copy pass on EVERY remaining text in `texts.py`
Apply the persian-doctor-copy principles: short sentences, state facts and conditions rather than
commands or blame, one verb per action, no system burden on the user, consistent terms across the bot.
Cut every message to the minimum that still answers "what happened / what now". Keep a table
old → new for the 25 most-seen texts in `docs/ZCODE_SHOPBOT_UX_DESIGN.md` so the owner can review wording.
`shopbot/test_texts_are_strings.py` must still pass.

### T7. Dead ends and errors
Free text that matches nothing → a friendly nudge + the menu (not silence or a long help dump).
Stale/expired inline buttons → "این دکمه قدیمی شده" + fresh menu. A "انصراف" path in every multi-step
flow that returns to the menu without leaving half-written state. Receipt step accepts photo, file,
or tracking code (already true) — make the prompt say so in one line.

### T8. Verification
- Update/add tests (`shopbot/test_*.py`) for every flow you changed; run all of them.
- Build a scripted walkthrough (python-telegram-bot test harness or the existing test style) for the
  9 journeys of T0 and print before/after tap counts in `docs/ZCODE_PROGRESS.md`.
- Persona review (as in the skill): a 55-year-old on a cheap phone, a first-time user who only wants
  the free trial, a customer who owes money and is annoyed, a reseller's family member who buys for 3
  people. For each, walk the flow and list any remaining confusion; fix or log it.
- Run the 20-hat review from the skill on the final menu + buy + account screens; log findings.

## 4. Part B — Remaining panel tasks (after Part A; read-only/no-owner-decision items only)
1. **Fix the 3 pre-existing failing backend tests** (`backend/tests/test_statement.py` "usage: month rows
   add up…", `test_notify_heartbeat.py`, `test_receipt_alert.py`): diagnose root cause per the
   diagnosing-bugs skill; fix the code or the test only if you can prove which one is wrong.
   Run from `backend/` with `python -m tests.<name>`.
2. **Weekly usage comparison** (read-only script, do NOT run against production; write it so the owner's
   assistant can run it with `ssh france-vpn 'docker exec -i -w /app marzban-admin-panel-backend-1
   python -W ignore -' < script.py`): last 7 days of usage vs each of the 4 previous weeks, per account
   and total, both in GB and in GB-weighted-by-price (configs on inbounds 6002, 6005 and the FR 6009
   tunnel count double: "ضریب ۲"). Source: `app/marzban_usage_log.py` / `app/statement.py` helpers;
   read them first. Output a compact table + top 10 movers.
3. **Doc hygiene**: list in `docs/ZCODE_PROGRESS.md` anything you found that looks wrong but is out of
   scope (do not fix).

## 5. NOT yours — owner decisions, do not do, only mention if relevant
Linking `real_seyed_tun` to Telegram; recording more payments for Seyed; group "mahan" package leftovers;
amirali_almasi 10.4k rate question; the 10 Marzban subscriptions with incomplete inbound sets; the US
tunnel (needs address/port/SNI/path from the owner, then its tag goes into `TUNNEL_INBOUND_TAGS`);
unlinked customer Azno; any deploy or live-data write.

## 6. Final report
When all tasks are done (or blocked), print: per task status, test commands + results, before/after tap
table, list of commits, the old→new copy table location, and the questions for the owner. Do not claim
"done" for anything you did not run.
