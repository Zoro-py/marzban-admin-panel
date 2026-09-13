# VPN Reseller Dashboard

A management layer on top of a Marzban panel: tracks who owns which account, who owes what
(and who's owed a credit), auto-renews and auto-bills accounts as they run low, and settles
pay-as-you-go groups (e.g. a company owner paying for employee accounts) — all backed by
Marzban's own API for usage/status, plus a local database for the business data Marzban has
no concept of (customers, ownership, money).

Three pieces, one shared backend:

```
backend/   FastAPI + SQLite (swap to Postgres later) — the source of truth, talks to Marzban
frontend/  Vite + React + Tailwind dashboard — full CRUD, live balances, invoices, charts
bot/       Telegram bot — quick mobile checks + the same actions as the dashboard
```

Public repo: `github.com/Zoro-py/marzban-admin-panel`. See `AGENTS.md` before making changes
— this panel moves real money, and that file documents the failure modes that have actually
happened here plus the discipline required to avoid repeating them. See `frontend/DESIGN.md`
for the UI's visual system. See `docs/DOMAIN_AND_BILLING.md` for the full billing/automation
reference — this file only covers running it.

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
- `MARZBAN_SUBSCRIPTION_BASE_URL` (optional) — only needed if your Marzban serves
  subscription links from a different public host than the panel. Left blank, a
  relative `/sub/<token>` from Marzban is resolved against `MARZBAN_BASE_URL`; an
  absolute subscription URL from Marzban is always used as-is.
- `BOT_TOKEN` / `BOT_ADMIN_CHAT_ID` (optional but recommended) — without these, every
  automatic Telegram notification (next-plan auto-queue/activation, payg cap-hit resets, the
  monthly payg settlement report, nightly backups) is silently skipped rather than sent. See
  `docs/DOMAIN_AND_BILLING.md` for why several of these features refuse to act at all if the
  notification can't be sent.
- `PAYG_MONTHLY_SETTLE_HOUR` / `PAYG_MONTHLY_SETTLE_MINUTE` (optional, default `23:30`
  server-local time) — when the daily check for "is a Jalali month ending tonight" runs.

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
`/credit <customer> <amount> [note]`, `/extend <username> <days> [gb]`,
`/bulk <name> <count> [30gb] [30d] [from=N]`, `/sync`,
`/backup` (on-demand DB backup, sent as a file to this chat).

`/bulk` previews the exact usernames and waits for a confirmation tap before
creating anything — see "Family batches" below.

Note: `BOT_TOKEN`/`BOT_ADMIN_CHAT_ID` in `backend/.env` are a **separate** thing from this
bot process — the backend uses them directly (via `app/notify.py`) to push automatic
notifications (next-plan, cap-hit, monthly settlement, nightly backup) to your chat, without
going through this bot's own polling loop at all. Point both at the same bot/chat in normal
use; the backend's notify path works even if this bot process isn't running.

## Family batches (many accounts, one name)

Selling a household or a small office several accounts at once: give a base
name and a count, and you get `khanevade1`, `khanevade2`, … all on the same
plan, plus one Telegram message per account carrying its QR code, its
subscription link and its username — each one ready to forward straight to the
person it belongs to.

- **Dashboard:** Accounts → *Family batch*. It shows the exact usernames it
  will create (and strikes through any that are already taken) *before* you
  commit, because creating a Marzban user cannot be undone.
- **Bot:** `/bulk khanevade 5 30gb 30d`. Same preview, as a confirm button.
- **Numbering** continues after the highest number already in use for that base
  name — a second batch for the same family carries on at `khanevade6` rather
  than colliding or restarting. `from=N` overrides that when you want specific
  numbers; names in that range that already exist are reported and skipped, and
  the rest keep the numbers you asked for.
- **Nothing is charged.** A batch creates accounts and can attach them to a
  customer or group, but posts no ledger entry — billing stays a separate,
  deliberate action, exactly as it is for a single account.
- **Partial failures are reported, never rolled back.** Each account is
  committed on its own, so if account 7 of 10 fails, accounts 1–6 really exist
  and the response says exactly which ones did and didn't. If Marzban itself
  goes down mid-batch, the rest are not attempted and the reason is shown.
- Up to 50 accounts per batch.

The QR/link messages are sent by the **backend** (using `BOT_TOKEN` /
`BOT_ADMIN_CHAT_ID` in `backend/.env`), not by the bot process — so they arrive
whether or not `bot.py` is running. With those unset, the accounts are still
created and both front-ends say plainly that no messages are coming.

## How ownership/billing works (short version)

- **Accounts** mirror Marzban users (created/synced via its API). Usage, limits, expiry,
  status all live in Marzban — this project never re-implements them, only mirrors a
  snapshot locally so the dashboard/bot don't hit Marzban on every page load.
- **Customers** are the people you actually deal with — a customer can own several accounts
  (e.g. one person, several family members' accounts).
- **Groups** are pay-as-you-go billing units (e.g. a company): several accounts billed
  together against one representative customer, on a recurring cycle.
- Every account/group is billed **prepay** (pay for a package up front, sized at sale time)
  or **payg** (metered — pay for what was actually used since the last settle). A group's
  mode governs every member's billing regardless of that member's own field.
- **Ledger** is an append-only transaction log (`charge` = debt owed to you, `credit` =
  payment received). A customer's or group's balance is always the sum of its ledger rows —
  never a field that gets overwritten, so there's a full audit trail.
- **Settle** posts a charge for what's currently owed and rolls the billing baseline forward
  — for payg, it also resets the account's actual usage in Marzban (the meter really reads 0
  after being billed for it). Never happens on its own; you (or an automatic job — see below)
  trigger it.
- Several things happen **automatically** without anyone clicking a button: renewing an
  account before it runs out (and billing the operator's chosen amount only after they
  approve it), auto-billing+resetting a payg account that hits a hard usage cap, and closing
  out every payg group/account on the last night of each real Jalali (Persian) calendar
  month. **Full details, thresholds, and the safety rules behind each of these are in
  `docs/DOMAIN_AND_BILLING.md`** — read it before touching any of `sync_job.py`,
  `payg_monthly_job.py`, or the settle/reset endpoints.
- A background job re-syncs every account's usage/status from Marzban on an interval
  (`SYNC_INTERVAL_SECONDS` in `backend/.env`, default 60); `POST /api/sync/run` or the bot's
  `/sync` trigger it immediately. This same cycle is what drives the auto-renew and cap-hit
  checks above.

## Deployment

Runs as three containers via `docker-compose.yml` — `backend` (port 8010), `bot`, and
`frontend` (nginx serving the static build, port 8011) — behind the **host's** nginx, which
terminates HTTPS for two subdomains and reverse-proxies to those two ports. Meant to run on
the **same server as Marzban** (needs network access to its API). Backend data persists in a
named Docker volume.

Two scripts, same idea as Marzban's own installer:
- `scripts/bootstrap.sh` — tiny, zero app logic. Its only job is making sure `git` is
  available, then cloning/pulling the repo and handing off to:
- `scripts/install.sh` — the real installer. Installs Docker/nginx/certbot, asks for config,
  sets up the nginx + Let's Encrypt reverse proxy, brings the containers up. **Safe to
  re-run**: every step (config, nginx, certs, containers) independently checks whether it's
  already done and skips or resumes accordingly, rather than one all-or-nothing gate — so
  re-running after any failure (e.g. certbot failing because DNS hadn't propagated yet)
  correctly retries only what's still needed. This is also what makes it reusable, unchanged,
  on any other server later.

The repo is public, so both scripts are fetched directly from
`raw.githubusercontent.com` — no Gist, no deploy key, no GitHub auth of any kind for cloning.

### Step 1 (server side) — the master command

```bash
sudo bash -c "$(curl -sL https://raw.githubusercontent.com/Zoro-py/marzban-admin-panel/main/scripts/bootstrap.sh)"
```

First run on a fresh server, this single line:
1. Installs `git` if missing (retries automatically if apt's lock is briefly held by
   background updates — common on a freshly booted VPS).
2. Clones the repo to `/opt/marzban-admin-panel` (plain HTTPS, no auth needed).
3. Hands off to `scripts/install.sh`, which installs Docker/nginx/certbot if missing, then
   asks for: the two subdomains (this deployment uses `ops.melobuds.ir` / `ops-api.melobuds.ir`
   — confirmed free via DNS lookup at the time; `admin.melobuds.ir` and anything with `vpn*`
   were avoided, either taken or intentionally not wanted in the name), your email for Let's
   Encrypt, your real Marzban admin URL/username/password, and your Telegram bot token/chat
   id — every field is validated non-blank before it moves on, so a stray blind Enter can't
   silently write an empty value into a `.env` file and fail confusingly later.
4. **Pauses**, printing this server's public IP — go do Step 2 before it requests SSL
   certificates.

Re-running the exact same command later (on this server or a new one) picks up exactly where
it left off — already cloned → straight to `install.sh`; already configured → straight to
whichever of nginx/certs/containers still needs doing.

**Routine redeploy on an already-set-up server** (e.g. after pulling a fix): from
`/opt/marzban-admin-panel`,

```bash
git pull && docker compose up -d --build
```

`docker compose ls` on the server will show every Compose project currently running
(`marzban` itself is a **separate** project/directory — never run compose commands from the
wrong one).

### Step 2 (your side, when the script pauses) — DNS

Cloudflare dashboard → `melobuds.ir` → DNS → **Add record**, twice:

| Type | Name | Content | Proxy status |
|---|---|---|---|
| A | `ops` | *(IP the script printed)* | DNS only (grey cloud) |
| A | `ops-api` | *(IP the script printed)* | DNS only (grey cloud) |

Grey-cloud (not proxied) for now — keeps the certificate request simple. Switch to proxied
(orange cloud) afterwards if you want Cloudflare's WAF in front of this too. Wait ~30–60s,
then back to the terminal, press Enter to let the script continue.

When it finishes: **`https://ops.melobuds.ir`**.

### Step 3 (your side, optional) — enable the one-click Actions deploy

A **second, separate** keypair — this one lets *GitHub Actions* SSH into the server (the
opposite direction from the deploy key above, which only lets the server pull *from* GitHub;
that key can't be reused here):

```bash
ssh-keygen -t ed25519 -C "gh-actions-deploy" -f ~/.ssh/gh_actions_deploy -N ""
cat ~/.ssh/gh_actions_deploy.pub >> ~/.ssh/authorized_keys
cat ~/.ssh/gh_actions_deploy       # copy this whole private key
```

Run these **on the server**, over the same SSH session you used for Step 1. Then, in a
browser: GitHub → repo → **Settings → Secrets and variables → Actions → New repository
secret**, four of them:
- `DEPLOY_HOST` — this server's IP
- `DEPLOY_USER` — `root`
- `DEPLOY_SSH_KEY` — the private key you just printed, pasted whole (including the
  `-----BEGIN/END-----` lines)
- `DEPLOY_PATH` — `/opt/marzban-admin-panel`

After that, **Actions tab → Deploy → Run workflow** SSHes in and runs `git pull && docker
compose up -d --build` for you — no manual server access needed for routine updates. Without
this step, redeploying just means running the routine-redeploy command above yourself.

### Reusing this on another server later

Exact same command as Step 1, on the new server — it'll ask for that server's own
subdomains/Marzban credentials/bot. Nothing here is hardcoded to one machine.

### The three "where's the backend" values, easy to mix up

- `bot/.env`'s `API_BASE_URL` → `http://backend:8000` (container-to-container, Compose's
  built-in service-name DNS — the installer sets this correctly automatically).
- root `.env`'s `PUBLIC_BACKEND_URL` → `https://ops-api.melobuds.ir`, i.e. what **your
  browser** reaches. Baked into the frontend at build time, so changing it needs a rebuild.
- `backend/.env`'s `MARZBAN_BASE_URL` → wherever Marzban's own API is already reachable.

### CI/CD

- **CI** (`.github/workflows/ci.yml`): every push/PR — backend + bot import-check, frontend
  typecheck + build. Catches breakage before it ever reaches the server.
- **Deploy** (`.github/workflows/deploy.yml`): **manual only** (`workflow_dispatch`) — a push
  to `main` never deploys by itself, given this touches a live panel and real billing data.
  Needs Step 3 above configured first.
