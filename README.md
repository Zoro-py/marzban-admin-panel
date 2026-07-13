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
   asks for: the two subdomains (defaults to `ops.melobuds.ir` / `ops-api.melobuds.ir` —
   confirmed free via DNS lookup; avoided `admin.melobuds.ir`, `vpn*`, since those either
   were taken or you didn't want "vpn" in the name), your email for Let's Encrypt, your real
   Marzban admin URL/username/password, and your Telegram bot token/chat id — every field is
   validated non-blank before it moves on, so a stray blind Enter can't silently write an
   empty value into a `.env` file and fail confusingly later.
4. **Pauses**, printing this server's public IP — go do Step 2 before it requests SSL
   certificates.

Re-running the exact same command later (on this server or a new one) picks up exactly where
it left off — already cloned → straight to `install.sh`; already configured → straight to
whichever of nginx/certs/containers still needs doing.

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
this step, redeploying just means running the Step 1 command again on the server yourself.

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
