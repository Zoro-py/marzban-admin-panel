#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vpn-monitor agent — one-shot collector, run every minute by systemd timer.

Reads /etc/vpn-monitor/agent.conf, samples the box, detects anomalies, and
POSTs {metrics, events} to the admin panel's /api/monitor/ingest. Every piece
is stdlib-only and Python-3.6-compatible ON PURPOSE: dallas-GThost-amin
(216.152.154.147) is a CentOS 7 box whose yum python3 is 3.6, and this agent
must run unmodified on all five servers.

Design constraints that shaped this file:
  * NEVER blocks for long: oneshot under a 60s timer, hard-fails gracefully.
  * Deltas, not absolutes: CPU/steal/net/retrans are computed against the
    previous run's counters kept in STATE_DIR/state.json, so a restart of the
    agent (or the box) just yields one skipped sample, never a garbage spike.
  * Events fire ONCE per incident via hysteresis latches (raise threshold A,
    clear threshold B < A) — the panel does no suppression, so all
    deduplication lives here.
  * The panel being unreachable must not lose data: unsent payloads spool to
    STATE_DIR/spool.jsonl (capped) and flush on a later successful run.
  * Local jsonl logs (LOG_DIR) are the post-mortem record of last resort.
    A SIZE-based budget (3GB per box; see LOG_BUDGET_MB below) is enforced
    by the agent itself: on crossing it, ~TRIM_CHUNK_MB of the OLDEST data
    is removed so fresh logs always have room.

Exit code is always 0 — a monitoring agent that spams cron/journal with
failures gets uninstalled; its own errors are events like everything else.
"""

from __future__ import print_function

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

CONF_PATH = os.environ.get("VPN_MONITOR_CONF", "/etc/vpn-monitor/agent.conf")
AGENT_VERSION = "vpn-monitor/1.2"

# ── the 3GB hard log budget (operator's rule, 2026-09-19):
# TOTAL budget per box is 3GB. The agent's own share (LOG_DIR + spool) is
# capped by SIZE, not by file count: when the total crosses LOG_BUDGET_MB,
# the agent frees TRIM_CHUNK_MB by deleting the OLDEST data first (rotated
# files, then the head of the active files). logrotate stays installed as a
# compression pass, but the size ceiling lives HERE so it behaves identically
# on every distro (CentOS 7 included).
LOG_BUDGET_MB = 2816   # 2.75 GiB of the 3GB; docker logs (~180MB) + panel DB take the rest
TRIM_CHUNK_MB = 250    # how much to free per eviction, per the operator's own example

# ── thresholds: (trigger, recover) — hysteresis so a value oscillating
# around one number doesn't machine-gun the events table.
STEAL_PCT = (10.0, 5.0)
MEM_AVAIL_PCT = (10.0, 15.0)          # trigger when available falls BELOW
DISK_USED_PCT = (85.0, 80.0)
CONNTRACK_USED_PCT = (90.0, 75.0)
TCP_RETRANS_PCT = (5.0, 2.0)
LOAD5_PER_CORE = (2.0, 1.0)           # multiplier on core count
PING_LOSS_PCT = (40.0, 0.0)           # trigger when loss rises ABOVE
PING_AVG_MS = (300.0, 200.0)
SPOOL_MAX_PAYLOADS = 300
PUSH_TIMEOUT_S = 12


def log_line(path, obj):
    """Append one json line; never raises (disk full != crash loop)."""
    try:
        with open(path, "a") as fh:
            fh.write(json.dumps(obj, separators=(",", ":")) + "\n")
    except Exception:
        pass


def iso_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_conf():
    conf = {}
    with open(CONF_PATH) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            conf[key.strip()] = value.strip()
    return conf


def load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)  # atomic on POSIX: reader never sees a half file


def _dir_size_bytes(paths):
    total = 0
    for p in paths:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def enforce_log_budget(log_dir, state_dir, budget_mb=None, trim_mb=None):
    """The 3GB rule's enforcer: if LOG_DIR + spool together exceed the
    budget (LOG_BUDGET_MB, overridable via agent.conf), free TRIM_CHUNK_MB
    starting from the OLDEST data — rotated files are deleted outright
    (they are pure history), and only if that is not enough does the HEAD
    of the active files get dropped (rewritten to their tail). Never
    raises: a budget enforcer that crashes the agent would stop the
    monitoring AND the enforcement in one move."""
    budget = (budget_mb if budget_mb is not None else LOG_BUDGET_MB) * 1024 * 1024
    trim = (trim_mb if trim_mb is not None else TRIM_CHUNK_MB) * 1024 * 1024
    try:
        candidates = []
        for d in (log_dir, state_dir):
            if not os.path.isdir(d):
                continue
            for name in os.listdir(d):
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    candidates.append(p)
        total = _dir_size_bytes(candidates)
        if total <= budget:
            return
        target = total - (budget - trim)
        # Oldest first: rotated/compressed history dies before anything live.
        # mtime, not name parsing — survives any rotation scheme.
        candidates.sort(key=lambda p: os.path.getmtime(p))
        for p in candidates:
            need = total - target
            if need <= 0:
                break
            name = os.path.basename(p)
            active = name in ("metrics.jsonl", "events.jsonl") and os.path.dirname(p) == log_dir
            if not active:
                total -= os.path.getsize(p)
                os.remove(p)
                continue
            # Active file: drop the head, keep the tail — exactly as much as
            # still needed, oldest file first (the operator's "remove from
            # the oldest until there's room"). temp+replace keeps the shrink
            # atomic for anything tailing the file, and the kept tail is
            # nudged forward to the next newline so no partial line survives
            # at the head of what remains.
            size = os.path.getsize(p)
            keep = size - min(size, need)
            if keep == 0:
                total -= size
                os.remove(p)
                continue
            with open(p, "rb") as fh:
                fh.seek(-keep, os.SEEK_END)
                tail = fh.read()
            nl = tail.find(bytes([10]))
            if 0 < nl < len(tail) - 1:
                tail = tail[nl + 1:]
            tmp = p + ".trim"
            with open(tmp, "wb") as fh:
                fh.write(tail)
            os.replace(tmp, p)
            total -= (size - len(tail))
    except Exception:
        pass


# ── metric collectors ───────────────────────────────────────────────────────

def read_proc_stat():
    with open("/proc/stat") as fh:
        for line in fh:
            if line.startswith("cpu "):
                vals = [int(v) for v in line.split()[1:10]]
                # user nice system idle iowait irq softirq steal
                idle = vals[3] + vals[4]
                total = sum(vals[:8])
                steal = vals[7]
                return {"total": total, "idle": idle, "steal": steal}
    return {"total": 0, "idle": 0, "steal": 0}


def cpu_cores():
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except AttributeError:  # py3.6 on CentOS: affinity works, but be safe
        try:
            with open("/proc/cpuinfo") as fh:
                return max(1, fh.read().count("processor\t:"))
        except Exception:
            return 1


def read_meminfo():
    info = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) >= 2 and parts[0].endswith(":"):
                try:
                    info[parts[0][:-1]] = int(parts[1])  # kB
                except ValueError:
                    pass
    total = info.get("MemTotal", 0)
    free = info.get("MemFree", 0)
    buffers = info.get("Buffers", 0)
    cached = info.get("Cached", 0) + info.get("SReclaimable", 0)
    avail = info.get("MemAvailable", max(0, total - free - buffers - cached))
    swap_used = max(0, info.get("SwapTotal", 0) - info.get("SwapFree", 0))
    return {
        "total_mb": total // 1024,
        "used_mb": max(0, total - free - buffers - cached) // 1024,
        "avail_mb": avail // 1024,
        "swap_used_mb": swap_used // 1024,
    }


def disk_used_pct(path="/"):
    try:
        st = os.statvfs(path)
        if st.f_blocks <= 0:
            return 0.0
        used = st.f_blocks - st.f_bfree
        return 100.0 * used / st.f_blocks
    except Exception:
        return 0.0


def default_iface():
    # Hex 00000000 destination in /proc/net/route = the default route.
    try:
        with open("/proc/net/route") as fh:
            for line in fh.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "00000000":
                    return parts[0]
    except Exception:
        pass
    return "eth0"


def read_netdev(iface):
    with open("/proc/net/dev") as fh:
        for line in fh.readlines()[2:]:
            if ":" not in line:
                continue
            name, _, rest = line.partition(":")
            if name.strip() == iface:
                vals = [int(v) for v in rest.split()]
                # rx: bytes packets errs drop ... tx starts at field 8
                return {
                    "rx_bytes": vals[0], "rx_errs": vals[2], "rx_drop": vals[3],
                    "tx_bytes": vals[8], "tx_errs": vals[10], "tx_drop": vals[11],
                }
    return None


def read_conntrack():
    out = {"count": 0, "max": 0}
    for key in ("count", "max"):
        path = "/proc/sys/net/netfilter/nf_conntrack_" + key
        if os.path.exists(path):
            try:
                with open(path) as fh:
                    out[key] = int(fh.read().strip())
            except Exception:
                pass
    return out


def read_tcp_segments():
    """Returns (outsegs, retranssegs) from /proc/net/snmp, or (0, 0)."""
    try:
        with open("/proc/net/snmp") as fh:
            lines = [l for l in fh if l.startswith("Tcp:")]
        if len(lines) < 2:
            return 0, 0
        header = lines[0].split()
        values = lines[1].split()
        mapping = dict(zip(header[1:], values[1:]))
        return int(mapping.get("OutSegs", 0)), int(mapping.get("RetransSegs", 0))
    except Exception:
        return 0, 0


def ping_target(target):
    """5 fast pings; returns {'loss': pct, 'avg_ms': ms} or None (no ping bin)."""
    cmd = ["ping", "-c", "5", "-W", "1", "-i", "0.2", "-q", target]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    except Exception:
        return None
    out = proc.stdout.decode("utf-8", "replace")
    loss = 100.0
    avg = None
    m = re.search(r"(\d+(?:\.\d+)?)% packet loss", out)
    if m:
        loss = float(m.group(1))
    m = re.search(r"= [\d.]+/([\d.]+)/", out)
    if m:
        avg = float(m.group(1))
    return {"loss": loss, "avg_ms": avg}


def docker_ps():
    """Returns ({name: status}, docker_ok). A box without docker is fine —
    WATCH_CONTAINERS is simply empty there."""
    if shutil.which("docker") is None:
        return {}, False
    try:
        proc = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        if proc.returncode != 0:
            return {}, False
        statuses = {}
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            if "\t" in line:
                name, _, status = line.partition("\t")
                statuses[name] = status
        return statuses, True
    except Exception:
        return {}, False


def marzban_node_events(container, state):
    """On the panel box only: watch Marzban's own log for node connection
    transitions — the single most direct answer to "why did users drop".
    Dedup via the last-seen log timestamp kept in state, because the docker
    --since window deliberately overlaps the cron interval."""
    if not container:
        return []
    events = []
    try:
        proc = subprocess.run(
            # --tail, not --since: --since re-parses the whole json log file
            # every run (O(size), and the panel log only grows); --tail reads
            # just the end. The last-seen-timestamp filter below absorbs the
            # overlap, so behavior is identical and much cheaper.
            ["docker", "logs", "--tail", "600", "--timestamps", container],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        raw = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    except Exception:
        return events
    last_seen = state.get("marzban_log_ts", "")
    max_seen = last_seen
    for line in raw.splitlines():
        m = re.match(r"^(\S+)\s", line)
        if not m:
            continue
        log_ts = m.group(1)
        if log_ts <= last_seen:
            continue
        m = re.search(r'Unable to connect to "([^"]+)" node', line)
        if m:
            events.append({"type": "node_connection_lost", "severity": "critical",
                           "detail": "marzban panel lost its connection to node '%s'" % m.group(1)})
        m = re.search(r'Connected to "([^"]+)" node', line)
        if m:
            events.append({"type": "node_connection_restored", "severity": "info",
                           "detail": "marzban panel reconnected to node '%s'" % m.group(1)})
        if log_ts > max_seen:
            max_seen = log_ts
    state["marzban_log_ts"] = max_seen
    return events


# ── event detection ─────────────────────────────────────────────────────────

def latch_check(latches, key, active, trigger_msg, recover_msg, severity):
    """The whole hysteresis mechanism in one place: emits at most one trigger
    event and one recovery event per incident, no matter how many minutes it
    spans."""
    events = []
    was = latches.get(key, False)
    if active and not was:
        events.append({"type": key, "severity": severity, "detail": trigger_msg})
        latches[key] = True
    elif not active and was:
        events.append({"type": key + "_recovered", "severity": "info", "detail": recover_msg})
        latches[key] = False
    return events


def detect_events(m, conf, state, containers_status, docker_ok, pings):
    events = []
    latches = state.setdefault("latches", {})
    cores = max(1, int(m.get("cpu_cores") or 1))

    # watched containers: absent from `docker ps` output = down
    watched = [c for c in conf.get("WATCH_CONTAINERS", "").split(",") if c.strip()]
    if docker_ok:
        events += latch_check(
            latches, "docker_unreachable", False, "", "", "critical")
        for name in watched:
            status = containers_status.get(name)
            down = status is None or not status.lower().startswith("up")
            events += latch_check(
                latches, "container_down:" + name, down,
                "container '%s' is not running (docker ps says: %s)" % (name, status or "absent"),
                "container '%s' is running again" % name,
                "critical")
    else:
        events += latch_check(
            latches, "docker_unreachable", True,
            "cannot talk to docker (docker ps failed) — container states unknown",
            "docker is reachable again", "critical")

    events += latch_check(
        latches, "cpu_steal_high", m["steal_pct"] >= STEAL_PCT[0],
        "cpu steal %.1f%% — the host is oversold or a neighbor is squeezing us" % m["steal_pct"],
        "cpu steal back down to %.1f%%" % m["steal_pct"], "warn")

    mem_avail_pct = 100.0 * m["mem_avail_mb"] / m["mem_total_mb"] if m["mem_total_mb"] else 100.0
    events += latch_check(
        latches, "mem_pressure", mem_avail_pct <= MEM_AVAIL_PCT[0],
        "only %.0f%% memory available (%dMB of %dMB)" % (mem_avail_pct, m["mem_avail_mb"], m["mem_total_mb"]),
        "memory available back up to %.0f%%" % mem_avail_pct, "warn")

    events += latch_check(
        latches, "disk_high", m["disk_used_pct"] >= DISK_USED_PCT[0],
        "root filesystem %.1f%% full" % m["disk_used_pct"],
        "root filesystem back down to %.1f%%" % m["disk_used_pct"], "warn")

    if m["conntrack_max"] > 0:
        used = 100.0 * m["conntrack_count"] / m["conntrack_max"]
        events += latch_check(
            latches, "conntrack_near_limit", used >= CONNTRACK_USED_PCT[0],
            "conntrack table %.0f%% full (%d/%d) — new connections will drop" % (used, m["conntrack_count"], m["conntrack_max"]),
            "conntrack usage back down to %.0f%%" % used, "warn")

    if m.get("_tcp_out_delta", 0) > 100:  # too little traffic for a honest ratio
        events += latch_check(
            latches, "tcp_retrans_high", m["tcp_retrans_pct"] >= TCP_RETRANS_PCT[0],
            "tcp retransmit ratio %.1f%% — the network path is sick" % m["tcp_retrans_pct"],
            "tcp retransmit ratio back down to %.1f%%" % m["tcp_retrans_pct"], "warn")

    load5 = m["load5"]
    events += latch_check(
        latches, "load_high", load5 > LOAD5_PER_CORE[0] * cores,
        "load5 %.2f on %d cores" % (load5, cores),
        "load5 back down to %.2f" % load5, "warn")

    for target, res in (pings or {}).items():
        if res is None:
            continue
        events += latch_check(
            latches, "ping_loss:" + target, res["loss"] >= PING_LOSS_PCT[0],
            "%.0f%% packet loss to %s" % (res["loss"], target),
            "packet loss to %s cleared" % target, "critical")
        if res["avg_ms"] is not None:
            events += latch_check(
                latches, "ping_latency:" + target, res["avg_ms"] >= PING_AVG_MS[0],
                "ping to %s averaged %.0fms" % (target, res["avg_ms"]),
                "ping to %s back under %.0fms" % (target, res["avg_ms"]), "warn")

    # NIC errors are rateless facts, not states: report the delta, no latch.
    if m["net_err_delta"] > 0 or m["net_drop_delta"] > 0:
        events.append({
            "type": "nic_errors", "severity": "info",
            "detail": "iface %s: +%d errors, +%d drops since last sample" % (
                m.get("_iface", "?"), m["net_err_delta"], m["net_drop_delta"])})
    return events


# ── push ────────────────────────────────────────────────────────────────────

def post_payload(url, token, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/api/monitor/ingest", data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Monitor-Token": token})
    with urllib.request.urlopen(req, timeout=PUSH_TIMEOUT_S) as resp:
        return resp.status == 200


def spool_write(spool_path, payload):
    if not (payload.get("metrics") or payload.get("events")):
        return
    try:
        lines = []
        if os.path.exists(spool_path):
            with open(spool_path) as fh:
                lines = fh.readlines()
        lines.append(json.dumps(payload, separators=(",", ":")) + "\n")
        # Cap by line count, not bytes: each line is one bounded payload.
        lines = lines[-SPOOL_MAX_PAYLOADS:]
        with open(spool_path, "w") as fh:
            fh.writelines(lines)
    except Exception:
        pass


def spool_flush(spool_path, url, token):
    """Send up to 20 spooled payloads oldest-first; stop at the first failure
    and keep the rest. Returns True if the spool is now empty."""
    if not os.path.exists(spool_path):
        return True
    with open(spool_path) as fh:
        lines = fh.readlines()
    remaining = list(lines)
    sent = 0
    for line in lines:
        if sent >= 20:
            break
        try:
            payload = json.loads(line)
            if post_payload(url, token, payload):
                remaining.remove(line)
                sent += 1
            else:
                break
        except Exception:
            break
    if remaining != lines:
        with open(spool_path, "w") as fh:
            fh.writelines(remaining)
    return not remaining


# ── main ────────────────────────────────────────────────────────────────────

def main():
    conf = read_conf()
    server_id = conf.get("SERVER_ID", "")
    panel_url = conf.get("PANEL_URL", "")
    token = conf.get("TOKEN", "")
    if not (server_id and panel_url and token):
        print("vpn-monitor: SERVER_ID/PANEL_URL/TOKEN missing in %s" % CONF_PATH, file=sys.stderr)
        return

    log_dir = conf.get("LOG_DIR", "/var/log/vpn-monitor")
    state_dir = conf.get("STATE_DIR", "/var/lib/vpn-monitor")
    for d in (log_dir, state_dir):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    state_path = os.path.join(state_dir, "state.json")
    spool_path = os.path.join(state_dir, "spool.jsonl")
    metrics_log = os.path.join(log_dir, "metrics.jsonl")
    events_log = os.path.join(log_dir, "events.jsonl")

    state = load_json(state_path, {})
    now = time.time()
    prev = state.get("prev", {})

    # ── sample everything
    stat = read_proc_stat()
    mem = read_meminfo()
    iface = default_iface()
    net = read_netdev(iface) or {"rx_bytes": 0, "rx_errs": 0, "rx_drop": 0,
                                 "tx_bytes": 0, "tx_errs": 0, "tx_drop": 0}
    ct = read_conntrack()
    tcp_out, tcp_re = read_tcp_segments()
    uptime_s = int(float(open("/proc/uptime").read().split()[0]))
    load1, load5, load15 = (float(x) for x in open("/proc/loadavg").read().split()[:3])

    dt = max(1.0, now - float(prev.get("ts", now - 60)))
    d_total = max(0, stat["total"] - int(prev.get("cpu_total", stat["total"])))
    d_idle = max(0, stat["idle"] - int(prev.get("cpu_idle", stat["idle"])))
    d_steal = max(0, stat["steal"] - int(prev.get("cpu_steal", 0)))
    cpu_pct = 100.0 * (d_total - d_idle) / d_total if d_total > 0 else 0.0
    steal_pct = 100.0 * d_steal / d_total if d_total > 0 else 0.0

    d_rx = max(0, net["rx_bytes"] - int(prev.get("rx_bytes", net["rx_bytes"])))
    d_tx = max(0, net["tx_bytes"] - int(prev.get("tx_bytes", net["tx_bytes"])))
    # keep rx/tx error and drop deltas separately for the dashboard
    err_d = max(0, (net["rx_errs"] + net["tx_errs"]) - int(prev.get("net_errs", net["rx_errs"] + net["tx_errs"])))
    drop_d = max(0, (net["rx_drop"] + net["tx_drop"]) - int(prev.get("net_drop", net["rx_drop"] + net["tx_drop"])))

    d_out = max(0, tcp_out - int(prev.get("tcp_out", tcp_out)))
    d_re = max(0, tcp_re - int(prev.get("tcp_re", tcp_re)))
    retrans_pct = 100.0 * d_re / d_out if d_out > 0 else 0.0

    pings = {}
    for target in [t for t in conf.get("PING_TARGETS", "").split(",") if t.strip()]:
        pings[target] = ping_target(target)

    containers_status, docker_ok = docker_ps()

    metrics = {
        "ts": iso_now(),
        "uptime_s": uptime_s,
        "load1": round(load1, 2), "load5": round(load5, 2), "load15": round(load15, 2),
        "cpu_cores": cpu_cores(),
        "cpu_pct": round(cpu_pct, 1),
        "steal_pct": round(steal_pct, 1),
        "mem_total_mb": mem["total_mb"], "mem_used_mb": mem["used_mb"],
        "mem_avail_mb": mem["avail_mb"], "swap_used_mb": mem["swap_used_mb"],
        "disk_used_pct": round(disk_used_pct(), 1),
        "net_rx_bps": round(d_rx * 8.0 / dt, 1),
        "net_tx_bps": round(d_tx * 8.0 / dt, 1),
        "net_err_delta": err_d,
        "net_drop_delta": drop_d,
        "conntrack_count": ct["count"], "conntrack_max": ct["max"],
        "tcp_retrans_pct": round(retrans_pct, 2),
        "extra": {
            "iface": iface,
            "docker_ok": docker_ok,
            "agent": AGENT_VERSION,
            "ping": {t: r for t, r in pings.items()},
            "containers": {name: containers_status.get(name, "absent")
                           for name in [c for c in conf.get("WATCH_CONTAINERS", "").split(",") if c.strip()]},
        },
    }

    # internal-only fields used by detect_events, stripped before sending
    metrics["_iface"] = iface
    metrics["_tcp_out_delta"] = d_out

    events = detect_events(metrics, conf, state, containers_status, docker_ok, pings)
    events += marzban_node_events(conf.get("MARZBAN_LOG_CONTAINER", ""), state)
    for ev in events:
        ev["ts"] = iso_now()

    # ── record locally (post-mortem record independent of the panel)
    log_line(metrics_log, {"ts": metrics["ts"], "server_id": server_id,
                           **{k: v for k, v in metrics.items() if not k.startswith("_")}})
    for ev in events:
        log_line(events_log, dict(ev, server_id=server_id))

    # ── ship
    payload = {"server_id": server_id, "metrics": metrics, "events": events}
    sendable = {"server_id": server_id,
                "metrics": {k: v for k, v in metrics.items() if not k.startswith("_")},
                "events": events}
    pushed = False
    try:
        spool_flush(spool_path, panel_url, token)
        pushed = post_payload(panel_url, token, sendable)
    except Exception as exc:
        log_line(events_log, {"ts": iso_now(), "server_id": server_id, "type": "agent_push_failed",
                              "severity": "warn", "detail": str(exc)[:300]})
    if not pushed:
        spool_write(spool_path, sendable)

    # ── advance state LAST: counters must move even when the push failed,
    # or the next run's deltas would double-count this interval.
    state["prev"] = {
        "ts": now, "cpu_total": stat["total"], "cpu_idle": stat["idle"],
        "cpu_steal": stat["steal"], "rx_bytes": net["rx_bytes"],
        "tx_bytes": net["tx_bytes"], "net_errs": net["rx_errs"] + net["tx_errs"],
        "net_drop": net["rx_drop"] + net["tx_drop"],
        "tcp_out": tcp_out, "tcp_re": tcp_re,
    }
    try:
        save_json_atomic(state_path, state)
    except Exception:
        pass

    # Budget enforcement runs last so a slow trim never delays sampling.
    enforce_log_budget(
        log_dir, state_dir,
        budget_mb=int(conf.get("LOG_BUDGET_MB") or 0) or None,
        trim_mb=int(conf.get("TRIM_CHUNK_MB") or 0) or None)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # last resort: an agent crash must be visible
        log_line("/var/log/vpn-monitor/events.jsonl",
                 {"ts": iso_now(), "type": "agent_crash", "severity": "critical", "detail": str(exc)[:300]})
        print("vpn-monitor agent crashed: %s" % exc, file=sys.stderr)
    sys.exit(0)
