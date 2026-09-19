# Server monitoring — agents + panel page

Answers two questions the operator actually asks:

1. **"وضعیت سرورها الان چطوره؟"** — one page in the admin panel (`/servers`)
   with CPU/steal/memory/disk/network/ping for all five boxes, live.
2. **«علت این قطعی‌های یهویی چیه؟»** — every anomaly (node connection lost,
   CPU steal spike, conntrack overflow, packet loss, container death, OOM
   memory pressure) lands in one timestamped event feed, so an outage is
   diagnosed by lining up timelines instead of guessing.

## Architecture

```
dallas-1 (216.106.179.122)  ─┐
dallas-2 (216.152.154.147)  ─┤ agent (systemd timer, every 60s)
amester  (217.60.26.124)    ─┤   POST /api/monitor/ingest  ← X-Monitor-Token
us-pazuki (94.183.182.254)  ┘        │
france-panel (95.182.83.238) ────────┤ (also parses Marzban's own container
                                     │  log for node connect/disconnect —
                                     │  the direct "flap" record)
                                     ▼
                     backend → SQLite (ServerMetric, MonitorEvent)
                                     ▼
                     GET /api/monitor/* → dashboard /servers page (JWT)
```

Push, not pull: no new listening port on any node (they sit behind who-knows-
what filtering), works regardless of DNS/Cloudflare, and a node whose agent
can't reach the panel spools locally and flushes later.

**Auth boundary:** agents hold only `MONITOR_INGEST_TOKEN` — a secret that
can INSERT metrics/events and nothing else. Never a dashboard JWT, never the
Marzban admin credentials. The dashboard read endpoints use the normal
operator JWT. Both fail closed when the token is unset.

## The 1GB log budget — HARD RULE

Logs (monitoring + docker) must never exceed **1GB per box**. Enforced in
three places, not by convention:

| Component | Mechanism | Worst case |
|---|---|---|
| agent jsonl logs (`/var/log/vpn-monitor/*.jsonl`) | logrotate `maxsize 50M`, `rotate 2`, compressed | ~300 MB |
| agent push spool (`/var/lib/vpn-monitor/spool.jsonl`) | capped at 300 payloads in code | ~5 MB |
| docker json-file logs (panel compose services) | `max-size: 10m`, `max-file: 3` × 6 services | ~180 MB |
| panel SQLite (`ServerMetric`/`MonitorEvent`) | ingest-side pruning: 30d metrics / 90d events | ~50 MB |

Per box total stays **under ~0.5 GB** even with everything at its cap. The
one component outside this repo's control is Marzban's own container log
(separate compose file); check it with
`du -sh /var/lib/docker/containers/*/*-json.log` and set the same `logging:`
block in its compose if it grows.

## Deploy

On the panel (France) first — the agents need something to talk to:

1. `MONITOR_INGEST_TOKEN=<python -c "import secrets; print(secrets.token_urlsafe(32))">`
   into `backend/.env` on the server, restart the backend container.
2. Deploy the new code (`git pull` + `docker compose build backend frontend
   && docker compose up -d`).

Then on each node, from a checkout of this repo:

```bash
scp scripts/monitor/vpn_monitor_agent.py scripts/monitor/install-agent.sh root@NODE:/tmp/
ssh root@NODE 'cd /tmp && chmod +x install-agent.sh && ./install-agent.sh \
  --server-id dallas-1 \
  --panel-url http://95.182.83.238:8010 \
  --token  <MONITOR_INGEST_TOKEN> \
  --ping-targets 95.182.83.238 \
  --watch-containers marzban-node'
```

Server ids in the current fleet: `france-panel`, `dallas-1` (216.106.179.122,
GThost), `dallas-2` (216.152.154.147, GThost-amin, **CentOS 7 / python3.6 —
the agent is written for 3.6, keep it that way**), `amester` (217.60.26.124),
`us-pazuki` (94.183.182.254). The France agent additionally gets
`--marzban-log-container marzban-marzban-1` and `--ping-targets` listing all
four node IPs, so a flap can be blamed on path vs. box by comparison.

## Event types

| type | severity | meaning |
|---|---|---|
| `node_connection_lost` / `_restored` | critical/info | Marzban panel lost/regained a node (from Marzban's own log) |
| `container_down:<name>` / `_recovered` | critical/info | watched docker container not running |
| `docker_unreachable` | critical | `docker ps` failing — states unknown |
| `cpu_steal_high` | warn | steal ≥10% — host oversold / noisy neighbor |
| `mem_pressure` | warn | available memory ≤10% |
| `disk_high` | warn | root fs ≥85% |
| `conntrack_near_limit` | warn | conntrack table ≥90% — new conns drop |
| `tcp_retrans_high` | warn | retransmit ratio ≥5% — sick network path |
| `ping_loss:<target>` | critical | ≥40% loss toward target |
| `ping_latency:<target>` | warn | ping avg ≥300ms |
| `load_high` | warn | load5 > 2× cores |
| `nic_errors` | info | NIC error/drop counters advanced |
| `agent_push_failed` / `agent_crash` | warn/critical | the agent's own health |

All triggers use hysteresis (fire at A, clear at B<A) so one incident is one
event pair, not sixty rows.

## Debugging an agent

```bash
systemctl list-timers vpn-monitor.timer      # is it scheduled?
journalctl -u vpn-monitor.service -n 50      # run output / errors
tail /var/log/vpn-monitor/metrics.jsonl      # what it last sampled
tail /var/log/vpn-monitor/events.jsonl       # what it last detected
wc -l /var/lib/vpn-monitor/spool.jsonl       # unsent payloads (0 = healthy)
```
