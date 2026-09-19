#!/usr/bin/env bash
# Install the vpn-monitor agent on ONE server. Run as root, with
# vpn_monitor_agent.py in the same directory as this script.
#
#   ./install-agent.sh --server-id dallas-1 \
#       --panel-url http://95.182.83.238:8010 \
#       --token <MONITOR_INGEST_TOKEN> \
#       [--ping-targets 95.182.83.238] \
#       [--watch-containers marzban-node] \
#       [--marzban-log-container marzban-marzban-1]
#
# Idempotent: safe to re-run (overwrites unit, conf, agent; keeps state/logs).
set -euo pipefail

SERVER_ID="" PANEL_URL="" TOKEN="" PING_TARGETS="" WATCH_CONTAINERS="" MARZBAN_CONTAINER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --server-id) SERVER_ID="$2"; shift 2 ;;
    --panel-url) PANEL_URL="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --ping-targets) PING_TARGETS="$2"; shift 2 ;;
    --watch-containers) WATCH_CONTAINERS="$2"; shift 2 ;;
    --marzban-log-container) MARZBAN_CONTAINER="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done
[[ -n "$SERVER_ID" && -n "$PANEL_URL" && -n "$TOKEN" ]] || { echo "--server-id, --panel-url, --token are required" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── python3: CentOS 7 (dallas-amin) ships without it; yum's is 3.6, which
# the agent is written for. Ubuntu boxes already have it.
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 missing — installing..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq python3
  elif command -v yum >/dev/null 2>&1; then
    yum install -y -q python3
  else
    echo "no apt/yum to install python3 with" >&2; exit 1
  fi
fi

# ── files
install -m 0755 "$SCRIPT_DIR/vpn_monitor_agent.py" /usr/local/bin/vpn-monitor-agent
mkdir -p /etc/vpn-monitor /var/log/vpn-monitor /var/lib/vpn-monitor
cat > /etc/vpn-monitor/agent.conf <<EOF
SERVER_ID=$SERVER_ID
PANEL_URL=$PANEL_URL
TOKEN=$TOKEN
PING_TARGETS=$PING_TARGETS
WATCH_CONTAINERS=$WATCH_CONTAINERS
MARZBAN_LOG_CONTAINER=$MARZBAN_CONTAINER
LOG_DIR=/var/log/vpn-monitor
STATE_DIR=/var/lib/vpn-monitor
EOF
chmod 0600 /etc/vpn-monitor/agent.conf   # holds the ingest token

# ── logrotate: the per-box share of the HARD 1GB log budget. Worst case
# here is ~300MB (2 files x 3 generations x 50M, compressed rotations are
# far smaller in practice) + ~5MB spool.
cat > /etc/logrotate.d/vpn-monitor <<'EOF'
/var/log/vpn-monitor/*.jsonl {
    daily
    maxsize 50M
    rotate 2
    compress
    missingok
    notifempty
    copytruncate
}
EOF

# ── systemd timer: every minute, oneshot. OnCalendar=minutely drifts with
# timer load; OnUnitActiveSec anchored to the last RUN is the steady one.
cat > /etc/systemd/system/vpn-monitor.service <<'EOF'
[Unit]
Description=VPN server monitoring agent (one-shot collector)

[Service]
Type=oneshot
ExecStart=/usr/local/bin/vpn-monitor-agent
TimeoutStartSec=60
EOF
cat > /etc/systemd/system/vpn-monitor.timer <<'EOF'
[Unit]
Description=Run vpn-monitor agent every minute

[Timer]
OnBootSec=30s
OnUnitActiveSec=60s
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now vpn-monitor.timer

# ── one manual run + show it worked
echo "--- manual test run:"
/usr/local/bin/vpn-monitor-agent && echo "agent run: OK" || echo "agent run: FAILED"
echo "--- ingest check (last events.log lines):"
tail -n 5 /var/log/vpn-monitor/events.jsonl 2>/dev/null || true
echo "--- timer:"
systemctl list-timers vpn-monitor.timer --no-pager | head -3
