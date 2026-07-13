# VPN Reseller Dashboard

A management layer on top of a Marzban panel: tracks who owns which account, who owes what
(and who's owed a credit), and settles pay-as-you-go groups (e.g. a company owner paying for
employee accounts) — all backed by Marzban's own API for usage/status, plus a local database
for the business data Marzban has no concept of (customers, ownership, money).

Three pieces, one shared backend:

```
backend/   FastAPI + SQLite (swap to Postgres later) — the source of truth, talks to Marzban
frontend/  Vite + React + Tailwind dashboard — full CRUD, live balances, invoices
bot/       Telegram bot — quick mobile checks + the same actions as the dashboard
```

Not yet a git repo — this is local, per-file dev for now; a repo gets created once this is
ready to be versioned.

## 1. Backend

```bash
cd backend
python -m venv venv
venv/Scripts/pip install -r requirements.txt   # (Windows; use venv/bin/pip on macOS/Linux)
cp .env.example .env
```

Edit `.env`:
- `MARZBAN_BASE_URL` / `MARZBAN_USERNAME` / `MARZBAN_PASSWORD` — a **sudo admin** account on
  your real Marzban panel. This is only a service account for the backend's own unattended
  calls (the nightly sync job runs with nobody logged in) — it is *not* a separate dashboard
  password. Logging into the web dashboard or the bot checks whatever you type directly
  against Marzban's own `/api/admin/token`, live, on every login — so any admin account
  Marzban itself accepts works everywhere here too, with nothing separate to invent.
- No `JWT_SECRET` to set — it's auto-generated into `backend/.jwt_secret` on first run.
- `MARZBAN_DEFAULT_PROXIES` / `MARZBAN_DEFAULT_INBOUNDS` (optional) — only needed if the
  built-in defaults (`vless`/`vmess`/`trojan`/`shadowsocks`, all inbounds) don't match how
  your panel's inbounds are actually tagged. Check `GET /api/inbounds` on your Marzban panel
  if new-account creation from the dashboard picks the wrong inbounds.

Run it:

```bash
venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
```

Visit `http://127.0.0.1:8000/docs` for the full interactive API reference (every endpoint
below the two front-ends use).

## 2. Frontend

```bash
cd frontend
npm install
cp .env.example .env   # VITE_API_BASE_URL — point at the backend above
npm run dev
```

Open `http://localhost:5173`, log in with your Marzban admin username + password.

Build for production: `npm run build` → static files in `frontend/dist/`.

## 3. Telegram bot

```bash
cd bot
python -m venv venv
venv/Scripts/pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:
- `BOT_TOKEN` — from [@BotFather](https://t.me/BotFather).
- `ADMIN_CHAT_ID` — your personal Telegram numeric chat id (message
  [@userinfobot](https://t.me/userinfobot) to get it). The bot ignores every other chat —
  it's single-operator by design since it moves real money and a live panel.
- `API_BASE_URL` — this backend.
- `MARZBAN_USERNAME` / `MARZBAN_PASSWORD` — the same Marzban admin credentials as in
  `backend/.env`; the bot uses them to log into the backend the same way the dashboard does.

Run it:

```bash
venv/Scripts/python bot.py
```

Commands: `/report`, `/customer <name or id>`, `/charge <customer> <amount> [note]`,
`/credit <customer> <amount> [note]`, `/extend <username> <days> [gb]`, `/sync`.

## How ownership/billing works

- **Accounts** mirror Marzban users (created/synced via its API). Usage, limits, expiry,
  status all live in Marzban — this project never re-implements them, only mirrors a
  snapshot locally so the dashboard/bot don't hit Marzban on every page load.
- **Customers** are the people you actually deal with — a customer can own several accounts
  (e.g. one person, several family members' accounts).
- **Groups** are pay-as-you-go billing units (e.g. a company): several accounts billed
  together against one representative customer, on a recurring cycle. `POST
  /api/groups/{id}/settle` charges the cycle's usage and rolls the baseline forward — call it
  whenever you're ready to bill (end of month, etc.), it doesn't happen automatically.
- **Ledger** is an append-only transaction log (`charge` = debt owed to you, `credit` =
  payment received). A customer's or group's balance is always the sum of its ledger rows —
  never a field that gets overwritten, so there's a full audit trail.
- A background job re-syncs every account's usage/status from Marzban on an interval
  (`SYNC_INTERVAL_MINUTES` in `backend/.env`, default 60); `POST /api/sync/run` or the bot's
  `/sync` trigger it immediately.

## Deployment note

The backend needs network access to your Marzban panel's API, so it's simplest to run it
**on the same server as Marzban** (or anywhere with a route to it) rather than on your local
machine. The frontend is a static build (`npm run build`) that can be served from anywhere
that can reach the backend — including opening it locally against a remote backend URL.
