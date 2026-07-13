#!/usr/bin/env bash
# Guided installer for the VPN reseller dashboard (backend + bot + frontend).
# Safe to re-run: if backend/.env already exists it offers to skip straight to
# a rebuild instead of re-asking every question, so this same script works for
# both the first install on a new server and redeploying on one already set up.
set -euo pipefail

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

ask() {
  local prompt="$1" default="${2:-}" reply
  read -rp "$prompt${default:+ [$default]}: " reply
  echo "${reply:-$default}"
}
ask_secret() {
  local prompt="$1" reply
  read -rsp "$prompt: " reply
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

# Port 80 is where certbot's HTTP-01 challenge and the new nginx server blocks both need
# to listen. A soft warning, not a hard stop — this box already runs Marzban, so it's worth
# flagging early if something unexpected already owns that port instead of failing deep
# inside the certbot step with a less obvious error.
check_port_80() {
  if ! command -v ss >/dev/null 2>&1; then
    return
  fi
  local holder
  holder=$(ss -tlnp 2>/dev/null | awk '/:80[[:space:]]/{print}')
  if [ -n "$holder" ] && ! echo "$holder" | grep -qi nginx; then
    warn "Something is already listening on port 80 that doesn't look like nginx:"
    echo "$holder"
    warn "If the certbot step below fails, this is the first thing to check."
  fi
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

  MARZBAN_BASE_URL=$(ask_required "Existing Marzban panel URL (e.g. https://sub.example.com:2096)")
  if [[ ! "$MARZBAN_BASE_URL" =~ ^https?:// ]]; then
    warn "No http(s):// scheme on that URL — assuming https://"
    MARZBAN_BASE_URL="https://$MARZBAN_BASE_URL"
  fi

  MARZBAN_USERNAME=$(ask_required "Marzban sudo admin username")
  MARZBAN_PASSWORD=$(ask_secret_required "Marzban sudo admin password")
  BOT_TOKEN=$(ask_secret_required "Telegram bot token (from @BotFather)")

  while true; do
    ADMIN_CHAT_ID=$(ask_required "Your Telegram numeric chat id (from @userinfobot)")
    [[ "$ADMIN_CHAT_ID" =~ ^-?[0-9]+$ ]] && break
    echo "That doesn't look like a numeric chat id (digits only, optionally starting with -)." >&2
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
}

write_env_files() {
  info "Writing backend/.env, bot/.env, .env"

  cat > backend/.env <<EOF
MARZBAN_BASE_URL=$MARZBAN_BASE_URL
MARZBAN_USERNAME=$MARZBAN_USERNAME
MARZBAN_PASSWORD=$MARZBAN_PASSWORD
DATABASE_URL=sqlite:////app/data/vpn.db
JWT_EXPIRE_MINUTES=1440
BOT_TOKEN=$BOT_TOKEN
BOT_ADMIN_CHAT_ID=$ADMIN_CHAT_ID
BOT_API_BASE_URL=http://backend:8000
SYNC_INTERVAL_MINUTES=60
EOF

  cat > bot/.env <<EOF
BOT_TOKEN=$BOT_TOKEN
ADMIN_CHAT_ID=$ADMIN_CHAT_ID
API_BASE_URL=http://backend:8000
MARZBAN_USERNAME=$MARZBAN_USERNAME
MARZBAN_PASSWORD=$MARZBAN_PASSWORD
EOF

  cat > .env <<EOF
PUBLIC_BACKEND_URL=https://$API_DOMAIN
EOF
}

write_nginx_configs() {
  info "Writing nginx server blocks"

  cat > /etc/nginx/sites-available/panel.conf <<EOF
server {
    listen 80;
    server_name $PANEL_DOMAIN;
    location / {
        proxy_pass http://127.0.0.1:8011;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

  cat > /etc/nginx/sites-available/api.conf <<EOF
server {
    listen 80;
    server_name $API_DOMAIN;
    location / {
        proxy_pass http://127.0.0.1:8010;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

  ln -sf /etc/nginx/sites-available/panel.conf /etc/nginx/sites-enabled/panel.conf
  ln -sf /etc/nginx/sites-available/api.conf /etc/nginx/sites-enabled/api.conf
  nginx -t
  systemctl reload nginx
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
