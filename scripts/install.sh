#!/usr/bin/env bash
# Guided installer for the VPN reseller dashboard (backend + bot + frontend).
# Safe to re-run: if backend/.env already exists it offers to skip straight to
# a rebuild instead of re-asking every question, so this same script works for
# both the first install on a new server and redeploying on one already set up.
set -euo pipefail
# Without this, `set -e` does NOT apply inside a $(...) command substitution
# by default — so a function called as `x=$(some_func)` that itself fails
# deep inside (e.g. hits EOF on `read`) only kills that inner subshell, not
# the whole script. Reproduced this exact bug (an `exit 1` on EOF inside ask()
# was silently swallowed, causing ask_required's retry loop to spin forever)
# and confirmed this line fixes it before relying on it.
shopt -s inherit_errexit

cd "$(dirname "$0")/.."   # always run from repo root, regardless of cwd

info() { echo -e "\n\033[1;34m==>\033[0m $1"; }
warn() { echo -e "\033[1;33mwarning:\033[0m $1"; }

# Retries a package-manager command a few times before giving up — apt-get can
# transiently fail with "Could not get lock /var/lib/dpkg/lock-frontend" on a
# freshly booted VPS still running unattended-upgrades in the background; common
# enough on cloud images to handle rather than fail the whole install over a race.
apt_retry() {
  local tries=0
  until "$@"; do
    tries=$((tries + 1))
    if [ "$tries" -ge 10 ]; then
      echo "'$*' kept failing after 10 tries (likely a stuck dpkg/apt lock)."
      echo "Check with: sudo lsof /var/lib/dpkg/lock-frontend"
      exit 1
    fi
    echo "apt is busy (dpkg lock?) — retrying in 5s... ($tries/10)"
    sleep 5
  done
}

# `read`'s exit status distinguishes "user pressed Enter on an empty line" (0)
# from "stdin closed / EOF" (non-zero) — without checking it, a closed stdin
# (broken SSH session, or this script run non-interactively by mistake) would
# make ask_required's retry loop spin forever printing "can't be blank."
# instead of failing once, clearly. Tested: reproduced the infinite spin,
# confirmed this stops it.
ask() {
  local prompt="$1" default="${2:-}" reply
  if ! read -rp "$prompt${default:+ [$default]}: " reply; then
    echo >&2
    echo "Input closed unexpectedly (EOF) — this installer needs an interactive terminal." >&2
    exit 1
  fi
  echo "${reply:-$default}"
}
ask_secret() {
  local prompt="$1" reply
  if ! read -rsp "$prompt: " reply; then
    echo >&2
    echo "Input closed unexpectedly (EOF) — this installer needs an interactive terminal." >&2
    exit 1
  fi
  echo >&2
  echo "$reply"
}
# Loops until non-empty input — for fields where an accidental blank (just
# pressing Enter) would otherwise get written straight into a .env file and
# fail confusingly much later (e.g. inside a container, or on first bot login)
# instead of right here where the user can immediately see and fix it.
ask_required() {
  local prompt="$1" reply
  while true; do
    reply=$(ask "$prompt")
    [ -n "$reply" ] && { echo "$reply"; return; }
    echo "This can't be blank." >&2
  done
}
ask_secret_required() {
  local prompt="$1" reply
  while true; do
    reply=$(ask_secret "$prompt")
    [ -n "$reply" ] && { echo "$reply"; return; }
    echo "This can't be blank." >&2
  done
}

# Escapes a value for safe placement inside double quotes in a .env file (both
# python-dotenv, used by the backend/bot, and Docker Compose's own env_file
# parser respect \\ and \" this way). Without this, a Marzban password/token
# containing '#', a space, or a quote could get silently truncated or corrupt
# the whole file — verified byte-for-byte against python-dotenv directly
# rather than assumed. Character-by-character on purpose: bash's `${v//\\/\\\\}`
# global pattern substitution does NOT reliably double a lone backslash (tested
# and confirmed broken), so this avoids that pattern-matching path entirely.
env_escape() {
  local s="$1" out="" c i
  for ((i = 0; i < ${#s}; i++)); do
    c="${s:i:1}"
    case "$c" in
      '\') out+='\\' ;;
      '"') out+='\"' ;;
      *) out+="$c" ;;
    esac
  done
  printf '%s' "$out"
}

require_root() {
  if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo bash scripts/install.sh"
    exit 1
  fi
}

require_debian_family() {
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "This installer targets Debian/Ubuntu (apt-get not found). Install Docker/nginx/certbot manually and run 'docker compose up -d --build' yourself."
    exit 1
  fi
}

install_docker() {
  if command -v docker >/dev/null 2>&1; then
    info "Docker already installed, skipping"
  else
    info "Installing Docker"
    curl -fsSL https://get.docker.com | sh
  fi
}

install_nginx_certbot() {
  if command -v nginx >/dev/null 2>&1 && command -v certbot >/dev/null 2>&1; then
    info "nginx + certbot already installed, skipping"
  else
    info "Installing nginx + certbot"
    apt_retry apt-get update -y
    apt_retry apt-get install -y nginx certbot python3-certbot-nginx
  fi
}

# Docker itself being present (checked above) does NOT guarantee the `docker compose`
# v2 plugin is — depends on how/when Docker was originally installed (e.g. Marzban's own
# installer may have set Docker up before this box ever needed Compose). Falls back to a
# directly-downloaded standalone binary rather than assuming a specific apt repo has the
# plugin package, so this works regardless of how Docker got there.
detect_or_install_compose() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
    info "Using docker compose (plugin)"
    return
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
    info "Using docker-compose (standalone binary, already installed)"
    return
  fi
  info "Neither 'docker compose' nor 'docker-compose' found — installing the standalone binary"
  # GitHub's release asset names are lowercase ("docker-compose-linux-x86_64"); `uname -s`
  # returns "Linux" (capital) on every real Linux box, which would 404 if used as-is.
  local os_lower arch
  os_lower=$(uname -s | tr '[:upper:]' '[:lower:]')
  arch=$(uname -m)
  curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-${os_lower}-${arch}" \
    -o /usr/local/bin/docker-compose
  chmod +x /usr/local/bin/docker-compose
  COMPOSE_CMD="docker-compose"
}

# Shared diagnostic used both as an early warning and, more importantly, at the actual
# point of failure if nginx can't bind a port — cross-references `ss` with `docker ps` so
# a port squatted by some other container (this box already runs Marzban) shows up by
# name instead of leaving it to guesswork.
diagnose_port() {
  local port="$1"
  echo "--- what's listening on port $port ---"
  if command -v ss >/dev/null 2>&1; then
    ss -tlnp 2>/dev/null | awk -v port="$port" '$0 ~ ":"port"[[:space:]]" {print}' || true
  fi
  if command -v docker >/dev/null 2>&1; then
    echo "--- docker containers publishing port $port ---"
    docker ps --format '{{.Names}}: {{.Ports}}' 2>/dev/null | grep -F ":${port}->" || echo "(none found via docker ps)"
  fi
}

# Port 80 is where certbot's HTTP-01 challenge and the new nginx server blocks both need
# to listen. A soft warning, not a hard stop — this box already runs Marzban, so it's worth
# flagging early if something unexpected already owns that port instead of only discovering
# it much later inside the certbot step.
check_port_80() {
  if ! command -v ss >/dev/null 2>&1; then
    return
  fi
  local holder
  holder=$(ss -tlnp 2>/dev/null | awk '/:80[[:space:]]/{print}') || true
  if [ -n "$holder" ] && ! echo "$holder" | grep -qi nginx; then
    warn "Something is already listening on port 80 that doesn't look like nginx:"
    diagnose_port 80
    warn "If nginx fails to start below, this is why — stop whatever that is first."
  fi
}

# `systemctl reload nginx` errors out ("nginx.service is not active, cannot reload") if
# nginx was installed but never actually came up — e.g. a fresh `apt-get install nginx`
# where the package's own postinst tried to start it and silently lost a port-80 race
# against something already there. `reload-or-restart` is the correct systemd verb for
# "reload if running, otherwise (re)start" and covers both a fresh install and a rerun.
start_or_reload_nginx() {
  if systemctl reload-or-restart nginx; then
    return
  fi
  echo
  echo "nginx failed to (re)start — almost always means something else already owns port 80 or 443."
  diagnose_port 80
  diagnose_port 443
  echo
  echo "Stop whatever that is (or move it off that port), then re-run the installer — it resumes from here."
  exit 1
}

collect_config() {
  info "Configuration"
  PANEL_DOMAIN=$(ask "Dashboard subdomain (what you open in a browser)" "ops.melobuds.ir")
  API_DOMAIN=$(ask "Backend API subdomain" "ops-api.melobuds.ir")
  if [ "$PANEL_DOMAIN" = "$API_DOMAIN" ]; then
    echo "Dashboard and API subdomains can't be the same value ($PANEL_DOMAIN)." >&2
    exit 1
  fi

  LE_EMAIL=$(ask_required "Email for Let's Encrypt renewal notices")

  verify_marzban_login

  # Format-validated, not just non-blank: pasting a secret into a masked
  # `read -s` prompt over SSH can get mangled by the terminal (bracketed-paste
  # artifacts have been observed to duplicate the token into itself) — this
  # writes silently-broken garbage into .env with no error until the bot
  # container crash-loops on "InvalidToken" much later. A real Telegram bot
  # token is always digits, a colon, then a 35-char secret — reject anything
  # else immediately, right where the operator can just retype it.
  while true; do
    BOT_TOKEN=$(ask_secret_required "Telegram bot token (from @BotFather)")
    [[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,40}$ ]] && break
    echo "That doesn't look like a Telegram bot token (expected digits:35-characters, e.g. 123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx)." >&2
    echo "If you pasted it, try typing it or pasting via your terminal's paste (not middle-click), then try again." >&2
  done

  while true; do
    ADMIN_CHAT_ID=$(ask_required "Your Telegram numeric chat id (from @userinfobot)")
    [[ "$ADMIN_CHAT_ID" =~ ^-?[0-9]+$ ]] && break
    echo "That doesn't look like a numeric chat id (digits only, optionally starting with -)." >&2
  done
}

# Collects MARZBAN_BASE_URL/USERNAME/PASSWORD and actually tests them against
# Marzban's own /api/admin/token before moving on. Catches, right here instead
# of after the whole install finishes: a URL that's the browser-facing
# dashboard path rather than the API root (e.g. https://host:port/dashboard/ —
# Marzban's API is always at the host root, any path typed here gets stripped
# with a warning), a typo'd password, or an unreachable host.
verify_marzban_login() {
  while true; do
    MARZBAN_BASE_URL=$(ask_required "Existing Marzban panel URL — host:port only, e.g. https://sub.example.com:2096 (not the /dashboard path)")
    if [[ ! "$MARZBAN_BASE_URL" =~ ^https?:// ]]; then
      warn "No http(s):// scheme on that URL — assuming https://"
      MARZBAN_BASE_URL="https://$MARZBAN_BASE_URL"
    fi
    if [[ "$MARZBAN_BASE_URL" =~ ^(https?://[^/]+)(/.*)?$ ]]; then
      if [ -n "${BASH_REMATCH[2]:-}" ]; then
        warn "Stripping '${BASH_REMATCH[2]}' — Marzban's API lives at the host root, not under a dashboard/sub-path"
      fi
      MARZBAN_BASE_URL="${BASH_REMATCH[1]}"
    fi

    MARZBAN_USERNAME=$(ask_required "Marzban sudo admin username")
    MARZBAN_PASSWORD=$(ask_secret_required "Marzban sudo admin password")

    info "Verifying against $MARZBAN_BASE_URL ..."
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
      -X POST "$MARZBAN_BASE_URL/api/admin/token" \
      --data-urlencode "username=$MARZBAN_USERNAME" \
      --data-urlencode "password=$MARZBAN_PASSWORD") || code="000"

    case "$code" in
      200)
        info "Marzban login verified."
        return
        ;;
      401)
        warn "Marzban rejected that username/password (HTTP 401). Let's try again."
        ;;
      000)
        warn "Couldn't reach $MARZBAN_BASE_URL at all (connection failed or timed out). Check the URL and try again."
        ;;
      *)
        warn "Got HTTP $code from $MARZBAN_BASE_URL/api/admin/token — that doesn't look like Marzban's API. Double-check the URL (host:port only, no path) and try again."
        ;;
    esac
  done
}

confirm_dns() {
  local ip
  ip=$(curl -fsSL -4 --max-time 5 ifconfig.me 2>/dev/null || curl -fsSL -4 --max-time 5 icanhazip.com 2>/dev/null || true)
  if [ -z "$ip" ]; then
    ip="<couldn't auto-detect — run 'curl -4 ifconfig.me' yourself>"
  fi
  info "Before continuing, make sure DNS is already pointing at this server:"
  echo "    $PANEL_DOMAIN  ->  A record  ->  $ip"
  echo "    $API_DOMAIN    ->  A record  ->  $ip"
  echo "  (Cloudflare: add both as 'DNS only' / grey-cloud for now — proxying them"
  echo "   can be turned on later once the certs are already issued.)"
  read -rp "Press Enter once both records exist and have propagated... "

  # Actually verify rather than trust the Enter keypress — catches "I added it
  # but it hasn't propagated yet" or a typo'd record BEFORE burning a
  # certbot attempt against Let's Encrypt's rate limits, not after.
  [ "$ip" = "<couldn't auto-detect — run 'curl -4 ifconfig.me' yourself>" ] && return
  if ! command -v getent >/dev/null 2>&1; then
    return
  fi

  local domain resolved tries
  for domain in "$PANEL_DOMAIN" "$API_DOMAIN"; do
    tries=0
    while true; do
      resolved=$(getent hosts "$domain" 2>/dev/null | awk '{print $1}' | head -1) || true
      if [ "$resolved" = "$ip" ]; then
        break
      fi
      tries=$((tries + 1))
      if [ "$tries" -ge 3 ]; then
        warn "$domain resolves to '${resolved:-nothing}', not $ip — certbot below will likely fail until this is fixed."
        break
      fi
      echo "  $domain -> '${resolved:-not resolving yet}', not $ip yet — waiting 10s and checking again ($tries/3)..."
      sleep 10
    done
  done
}

write_env_files() {
  info "Writing backend/.env, bot/.env, .env"

  # Quoted + escaped so a password/token containing '#', a space, or a quote
  # can't get silently truncated or corrupt the file (verified against
  # python-dotenv directly — see env_escape above).
  local q_url q_user q_pass q_token q_chat
  q_url=$(env_escape "$MARZBAN_BASE_URL")
  q_user=$(env_escape "$MARZBAN_USERNAME")
  q_pass=$(env_escape "$MARZBAN_PASSWORD")
  q_token=$(env_escape "$BOT_TOKEN")
  q_chat=$(env_escape "$ADMIN_CHAT_ID")

  cat > backend/.env <<EOF
MARZBAN_BASE_URL="$q_url"
MARZBAN_USERNAME="$q_user"
MARZBAN_PASSWORD="$q_pass"
DATABASE_URL=sqlite:////app/data/vpn.db
JWT_EXPIRE_MINUTES=1440
BOT_TOKEN="$q_token"
BOT_ADMIN_CHAT_ID="$q_chat"
BOT_API_BASE_URL=http://backend:8000
SYNC_INTERVAL_MINUTES=60
EOF

  cat > bot/.env <<EOF
BOT_TOKEN="$q_token"
ADMIN_CHAT_ID="$q_chat"
API_BASE_URL=http://backend:8000
MARZBAN_USERNAME="$q_user"
MARZBAN_PASSWORD="$q_pass"
EOF

  cat > .env <<EOF
PUBLIC_BACKEND_URL=https://$API_DOMAIN
EOF
}

# Writes one server block for a domain — with a live listen-443-ssl block
# reusing an already-issued cert if one exists, or listen-80-only (serving
# the app directly, not just redirecting, since this also needs to work
# *before* a cert exists — certbot's HTTP-01 challenge needs port 80 to
# already be serving something real) otherwise.
#
# cert_dir is passed in rather than derived from `domain` — request_certs
# calls `certbot -d PANEL_DOMAIN -d API_DOMAIN` together, so certbot issues
# ONE certificate covering both as SANs and stores it under only the FIRST
# domain's directory; there is no separate .../live/API_DOMAIN/. Deriving
# cert_dir from each call's own domain (an earlier version of this function
# did) meant the check always failed for the API domain specifically, so its
# vhost silently got no 443 block at all — nginx then fell back to treating
# the *panel* vhost's 443 block as the implicit default for the port, and
# every HTTPS request to the API domain got proxied to the frontend
# container instead of the backend. Confirmed live: an OPTIONS preflight to
# the API domain came back from the frontend's own nginx (port 8011), not
# the backend, which is exactly what that fallback produces.
#
# This function is what write_nginx_configs re-runs on every single install.sh
# invocation, including a routine redeploy where a cert already exists from
# a previous run — so it must independently reconstruct the exact same SSL
# config certbot would have written, every time, rather than only writing the
# plain HTTP block and assuming SSL config, once added, stays untouched.
_write_site_config() {
  local domain="$1" upstream_port="$2" conf_name="$3" cert_dir="$4"

  if [ -f "$cert_dir/fullchain.pem" ]; then
    cat > "/etc/nginx/sites-available/${conf_name}.conf" <<EOF
server {
    listen 80;
    server_name $domain;
    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl;
    server_name $domain;

    ssl_certificate $cert_dir/fullchain.pem;
    ssl_certificate_key $cert_dir/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:$upstream_port;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
  else
    cat > "/etc/nginx/sites-available/${conf_name}.conf" <<EOF
server {
    listen 80;
    server_name $domain;
    location / {
        proxy_pass http://127.0.0.1:$upstream_port;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
  fi
}

write_nginx_configs() {
  info "Writing nginx server blocks"

  # Both vhosts share one certificate (see request_certs — issued for both
  # domains together), stored under the first (panel) domain's directory.
  local cert_dir="/etc/letsencrypt/live/$PANEL_DOMAIN"

  _write_site_config "$PANEL_DOMAIN" 8011 panel "$cert_dir"
  _write_site_config "$API_DOMAIN" 8010 api "$cert_dir"

  ln -sf /etc/nginx/sites-available/panel.conf /etc/nginx/sites-enabled/panel.conf
  ln -sf /etc/nginx/sites-available/api.conf /etc/nginx/sites-enabled/api.conf
  nginx -t
  start_or_reload_nginx
}

request_certs() {
  info "Requesting Let's Encrypt certificates"
  certbot --nginx -d "$PANEL_DOMAIN" -d "$API_DOMAIN" --non-interactive --agree-tos -m "$LE_EMAIL" --redirect
}

compose_up() {
  info "Building and starting containers"
  $COMPOSE_CMD up -d --build
}

STATE_FILE=".install_state"

load_state() {
  [ -f "$STATE_FILE" ] || return 1
  # shellcheck disable=SC1090
  source "$STATE_FILE"
  [ -n "${PANEL_DOMAIN:-}" ] && [ -n "${API_DOMAIN:-}" ] && [ -n "${LE_EMAIL:-}" ]
}

save_state() {
  cat > "$STATE_FILE" <<EOF
PANEL_DOMAIN=$PANEL_DOMAIN
API_DOMAIN=$API_DOMAIN
LE_EMAIL=$LE_EMAIL
EOF
}

main() {
  require_root
  require_debian_family
  install_docker
  detect_or_install_compose
  check_port_80
  install_nginx_certbot

  # Each remaining step below checks its own already-done state independently
  # (config collected? nginx written? cert obtained?) rather than one coarse
  # "does backend/.env exist" gate — so re-running after a failure partway
  # through (e.g. certbot failing because DNS hadn't propagated yet) correctly
  # retries exactly the step that failed instead of silently skipping it.
  if [ -f backend/.env ] && load_state; then
    info "Reusing existing configuration for $PANEL_DOMAIN (delete backend/.env and $STATE_FILE to start over)"
  else
    collect_config
    confirm_dns
    write_env_files
    save_state
  fi

  write_nginx_configs

  if [ -f "/etc/letsencrypt/live/$PANEL_DOMAIN/fullchain.pem" ]; then
    info "Certificate for $PANEL_DOMAIN already exists, skipping certbot"
  else
    request_certs
  fi

  compose_up

  info "Done."
  echo "Dashboard: https://$PANEL_DOMAIN"
  echo "API docs:  https://$API_DOMAIN/docs"
}

main
