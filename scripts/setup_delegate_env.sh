#!/usr/bin/env bash
# Sets up DELEGATE_BOT_API_KEY (shared secret) in both backend/.env and
# delegate_bot/.env, and wires DELEGATE_BOT_TOKEN into delegate_bot/.env.
# Idempotent — safe to re-run; re-running replaces an existing key/token
# with what you pass, everything else in each .env file is left untouched.
#
# Usage (from the repo root, e.g. /opt/marzban-admin-panel):
#   ./scripts/setup_delegate_env.sh <DELEGATE_BOT_TOKEN from @BotFather>
set -euo pipefail

if [ -z "${1:-}" ]; then
  echo "Usage: $0 <DELEGATE_BOT_TOKEN>"
  echo "Get a token first: message @BotFather on Telegram, /newbot, then paste the token here."
  exit 1
fi
DELEGATE_BOT_TOKEN="$1"

if [ ! -d backend ] || [ ! -d delegate_bot ]; then
  echo "Run this from the repo root (backend/ and delegate_bot/ must exist here)."
  exit 1
fi

# Same generation method as scripts/install.sh's own shop_key, so this
# doesn't depend on python3 being installed on the server.
API_KEY="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"

# delegate_bot/.env — create from the example if it doesn't exist yet.
if [ ! -f delegate_bot/.env ]; then
  cp delegate_bot/.env.example delegate_bot/.env
fi
# Idempotent set-or-append for a KEY=value line.
set_env() {
  local file="$1" key="$2" value="$3"
  if grep -q "^${key}=" "$file"; then
    sed -i "s#^${key}=.*#${key}=${value}#" "$file"
  else
    echo "${key}=${value}" >> "$file"
  fi
}
set_env delegate_bot/.env DELEGATE_BOT_TOKEN "$DELEGATE_BOT_TOKEN"
set_env delegate_bot/.env DELEGATE_BOT_API_KEY "$API_KEY"
# API_BASE_URL inside delegate_bot/.env is overridden by docker-compose.yml's
# own `environment:` block at container start, so it's left as whatever the
# .env.example default already put there — no need to touch it here.

if [ ! -f backend/.env ]; then
  echo "backend/.env doesn't exist — set up the backend first (see README's Backend section), then re-run this."
  exit 1
fi
set_env backend/.env DELEGATE_BOT_API_KEY "$API_KEY"

echo "Done."
echo "  delegate_bot/.env : DELEGATE_BOT_TOKEN + DELEGATE_BOT_API_KEY set"
echo "  backend/.env      : DELEGATE_BOT_API_KEY set (matches delegate_bot's)"
echo ""
echo "Next: docker compose up -d --build"
echo "Then, from your own bot: /delegate_add <their telegram_id> <customer name> [credit_limit]"
