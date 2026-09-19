# GThost support ticket — 2026-09-19

**Status:** ready to submit (paste into GThost client area for BOTH VMs, or
link one ticket to both services).
**Copy-paste subject:** `Repeated ~60s total network path loss on two Dallas VMs — evidence attached, please check host/vSwitch`

> NOTE (for us, not for the ticket): deliberately framed as generic
> "production services" — no VPN/Xray/proxy wording anywhere, per ops policy.
> Incident data lives in the panel DB (`monitorevent`) and per-box agent logs.

---

Subject: Repeated ~60s total network path loss on two Dallas VMs — evidence attached, please check host/vSwitch

Hello,

We run production services on two of your Dallas VMs (reverse-proxied
web/API traffic):

- VM A: 216.106.179.122 (122-179-106-216.clients.gthost.com)
- VM B: 216.152.154.147 (147-154-152-216.clients.gthost.com)

Since we deployed continuous 1-minute monitoring on 2026-09-18, both VMs are
repeatedly losing their ENTIRE network path for ~60 seconds at a time. This
is measured independently from BOTH ends — a monitoring server in France
(95.182.83.238) pings each VM every minute, and each VM pings the monitoring
server back every minute. Every incident shows:

1. **100% packet loss in BOTH directions simultaneously** (France→VM and
   VM→France), e.g. 2026-09-19 08:05:36 UTC (VM A) — while VM A's own agent
   also timed out pushing its metrics to France in the same minute.
2. **No link flap inside the VM**: the Linux kernel on both VMs logs zero
   NIC/link down/up events (journalctl empty of carrier events), and eth0
   lifetime error counters are 0. So the drop is upstream of the VM's virtual
   NIC — host, vSwitch, or upstream network — not inside our VM.
3. **Incidents hit the two VMs in separate windows**, despite them being in
   the same DC (VM A ↔ VM B ping is 0.6 ms clean), which points at
   per-host/per-VM networking rather than a single upstream circuit.
4. **CPU steal spikes on the same host at incident time** (VM A showed 11–13%
   steal within the same minutes), consistent with a busy host node.
5. After each ~60s blackout, connectivity returns fully on its own.

Incident timestamps (UTC, 2026-09-19, from our event log):

- VM A (216.106.179.122): 05:40:40, 06:09:19, 06:34:51, 07:33:46, 08:05:36,
  08:27:01 — each ~60–64s, 100% loss both directions
- VM B (216.152.154.147): 08:28:55, 08:48:35 — same signature

The same signature was already visible in our control-plane logs for at least
24h before formal monitoring (roughly 1–6 incidents per hour, both VMs,
all times of day), so this is chronic, not a one-off.

Each incident takes our services offline for our users and forces the central
server to restart its service processes on the affected VM when the link
returns, so the customer impact is longer than the 60s blackout itself.

**What we ask:**

1. Please check the physical host / vSwitch / upstream port for BOTH VMs —
   especially VM A (216.106.179.122), which had 51 of the 75 incidents in the
   last 24h — for errors, flapping, or a noisy-neighbor situation, and migrate
   or re-home the VM if the host is problematic.
2. Please confirm whether any DDoS-mitigation / scrubbing / rate-limiting
   applies to these IPs that could be quiescing traffic for ~60s at a time.
3. Optional but useful: quote us a RAM upgrade for VM A (currently 1 GB) — we
   observed a single OOM-kill on it under a short allocation burst. (A third
   VM we just provisioned in Detroit, 216.152.153.118, will get the same
   monitoring — if its data shows the same pattern we'll follow up on that
   service too.)

We keep 1-minute-resolution metrics (ping loss/latency, retransmits, NIC
counters) for all our servers and will gladly provide raw data for any window
you investigate.

Thank you,
Mohammad Hossein
