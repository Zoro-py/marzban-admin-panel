#!/usr/bin/env bash
# Public entry point for installing marzban-admin-panel — meant to be fetched
# with a single curl|bash command, the same way Marzban's own installer works.
# Now that the repo itself is public, this can live in the repo and be fetched
# straight from raw.githubusercontent.com — no separate Gist to keep in sync,
# no SSH keys, no GitHub auth of any kind. Its only job is to make sure git is
# available, clone/pull the repo, and hand off to scripts/install.sh, which
# does everything else (Docker, nginx, certbot, .env, docker compose).
set -euo pipefail

REPO_URL="https://github.com/Zoro-py/marzban-admin-panel.git"
INSTALL_DIR="/opt/marzban-admin-panel"

if [ "$EUID" -ne 0 ]; then
  echo "Run as root, e.g.:"
  echo '  sudo bash -c "$(curl -sL <raw-bootstrap.sh-url>)"'
  exit 1
fi

# Retries a package-manager command a few times before giving up — apt-get can
# transiently fail with "Could not get lock /var/lib/dpkg/lock-frontend" on a
# freshly booted VPS still running unattended-upgrades in the background; this
# is common enough on cloud images that it's worth handling rather than just
# failing the whole install over a timing race.
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

if ! command -v git >/dev/null 2>&1; then
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "git is missing and this isn't a Debian/Ubuntu box (no apt-get) — install git manually, then re-run this command."
    exit 1
  fi
  echo "==> Installing git"
  apt_retry apt-get update -y -qq
  apt_retry apt-get install -y -qq git
fi

if [ -d "$INSTALL_DIR/.git" ]; then
  echo "==> $INSTALL_DIR already exists — pulling latest instead of cloning"
  git -C "$INSTALL_DIR" pull --ff-only
else
  if [ -e "$INSTALL_DIR" ]; then
    # Only reachable if a previous attempt got interrupted before the clone
    # ever completed (no .git yet) — nothing of value can exist there yet,
    # since scripts/install.sh (which is what would eventually write real
    # config) only exists once the clone succeeds. Safe to clear and retry.
    echo "==> $INSTALL_DIR exists but isn't a complete clone (leftover from an interrupted run) — removing it and retrying"
    rm -rf "$INSTALL_DIR"
  fi
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

exec bash "$INSTALL_DIR/scripts/install.sh"
