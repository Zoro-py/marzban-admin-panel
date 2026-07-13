#!/usr/bin/env bash
# Public entry point for installing the (private) marzban-admin-panel repo —
# this file is meant to be published somewhere public (a Gist) so it can be
# fetched with a single curl|bash command, the same way Marzban's own
# installer works. It contains zero secrets and zero app logic: its only job
# is to get an SSH deploy key set up once, clone/pull the private repo, and
# hand off to that repo's own scripts/install.sh, which does everything else
# (Docker, nginx, certbot, .env, docker compose) and lives under normal
# version control where it can be reviewed and changed like any other code.
set -euo pipefail

REPO_SSH_URL="git@github.com:Zoro-py/marzban-admin-panel.git"
INSTALL_DIR="/opt/marzban-admin-panel"

if [ "$EUID" -ne 0 ]; then
  echo "Run as root, e.g.:"
  echo '  sudo bash -c "$(curl -sL <this-gist-raw-url>)"'
  exit 1
fi

if [ ! -f ~/.ssh/id_ed25519 ]; then
  echo "==> Generating an SSH key so this server can pull the private repo"
  ssh-keygen -t ed25519 -C "$(hostname)-marzban-admin-panel" -f ~/.ssh/id_ed25519 -N ""
fi

if [ -d "$INSTALL_DIR/.git" ]; then
  echo "==> $INSTALL_DIR already exists — pulling latest instead of cloning"
  git -C "$INSTALL_DIR" pull
else
  echo
  echo "==> One-time step: add this server's public key as a read-only Deploy Key"
  echo "    github.com/Zoro-py/marzban-admin-panel -> Settings -> Deploy keys -> Add deploy key"
  echo
  cat ~/.ssh/id_ed25519.pub
  echo
  read -rp "Press Enter once it's added on GitHub... "

  ssh-keyscan -H github.com >> ~/.ssh/known_hosts 2>/dev/null
  git clone "$REPO_SSH_URL" "$INSTALL_DIR"
fi

exec bash "$INSTALL_DIR/scripts/install.sh"
