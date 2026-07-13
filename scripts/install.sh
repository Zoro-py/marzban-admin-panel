#!/usr/bin/env bash
# Guided installer for the VPN reseller dashboard (backend + bot + frontend).
# Safe to re-run: if backend/.env already exists it offers to skip straight to
# a rebuild instead of re-asking every question, so this same script works for
# both the first install on a new server and redeploying on one already set up.
set -euo pipefail

cd "$(dirname "$0")/.."   # always run from repo root, regardless of cwd

info() { echo -e "\n\033[1;34m==>\033[0m $1"; }
warn() { echo -e "\033[1;33mwarning:\033[0m $1"; }
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
    apt-get update -y
    apt-get install -y nginx certbot python3-certbot-nginx
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
  info "Configuration (blank keeps the current value if reconfiguring)"
  PANEL_DOMAIN=$(ask "Dashboard subdomain (what you open in a browser)" "ops.melobuds.ir")
  API_DOMAIN=$(ask "Backend API subdomain" "ops-api.melobuds.ir")
  LE_EMAIL=$(ask "Email for Let's Encrypt renewal notices")
  MARZBAN_BASE_URL=$(ask "Existing Marzban panel URL (e.g. https://sub.example.com:2096)")
  MARZBAN_USERNAME=$(ask "Marzban sudo admin username")
  MARZBAN_PASSWORD=$(ask_secret "Marzban sudo admin password")
  BOT_TOKEN=$(ask_secret "Telegram bot token (from @BotFather)")
  ADMIN_CHAT_ID=$(ask "Your Telegram numeric chat id (from @userinfobot)" "")
}

confirm_dns() {
  local ip
  ip=$(curl -fsSL -4 ifconfig.me || echo "<could not detect>")
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

main() {
  require_root
  require_debian_family
  install_docker
  detect_or_install_compose
  check_port_80
  install_nginx_certbot

  if [ -f backend/.env ]; then
    warn "backend/.env already exists — this server looks already configured."
    local reconfigure
    reconfigure=$(ask "Reconfigure from scratch? (y/N)" "N")
    if [[ ! "$reconfigure" =~ ^[Yy]$ ]]; then
      info "Skipping configuration, just rebuilding and restarting containers"
      compose_up
      info "Done."
      exit 0
    fi
  fi

  collect_config
  confirm_dns
  write_env_files
  write_nginx_configs
  request_certs
  compose_up

  info "Done."
  echo "Dashboard: https://$PANEL_DOMAIN"
  echo "API docs:  https://$API_DOMAIN/docs"
}

main
