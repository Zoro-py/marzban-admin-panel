"""READ-ONLY — weekly usage comparison (shopbot master prompt, Part B-2, 2026-11-06).

Run by the owner's assistant on the live box (NEVER from a laptop; the panel
DB and the Marzban DB live there):

  ssh france-vpn 'docker exec -i -w /app marzban-admin-panel-backend-1 python -W ignore -' \
      < scripts/weekly_usage_comparison_2026-11-06.py

What it answers: last 7 days of real usage vs each of the 4 previous 7-day
windows, per account and in total — once in raw GB, once weighted
(GB × rate, ×2 for accounts whose Marzban inbounds sit on port 6002 / 6005
or the FR 6009 tunnel — «ضریب ۲»). Compact table + the top-10 movers.

Nothing here writes: panel DB SELECT only, Marzban sqlite through
marzban_usage_log (mode=ro), Marzban API GET only. Usage figures reuse the
statement's own rules (app/statement.py): exact from the reset log when the
period sits fully inside a window, time-prorated when a closed period cuts a
window edge, and the live period's share from Marzban's per-node history —
so the numbers here and on the panel's statement page can be compared.

An account whose numbers could not be read (Marzban 404 / API hiccup) is
marked «?» and left out of the totals, never guessed.
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, "/app")

from sqlmodel import Session, select  # noqa: E402

from app.db import engine  # noqa: E402
from app.marzban_client import (  # noqa: E402
    MarzbanAuthError,
    MarzbanUnavailable,
    marzban_client,
)
from app.marzban_usage_log import load_periods  # noqa: E402
from app.models import Account, Group  # noqa: E402
from app.statement import _prorate  # noqa: E402

GB = 1024 ** 3
WINDOWS = 5                      # W4 W3 W2 W1 W0  (W0 = the last 7 days)
DOUBLE_PORTS = {"6002", "6005", "6009"}   # D servers + FR tunnel → «ضریب ۲»
TOP_MOVERS = 10


def naive(dt: datetime) -> datetime:
    """statement.py's convention: naive UTC everywhere."""
    return dt.replace(tzinfo=None)


async def main() -> None:
    now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    edges = [now - timedelta(days=7 * i) for i in range(WINDOWS, -1, -1)]  # oldest → newest
    windows = [(naive(edges[i]), naive(edges[i + 1])) for i in range(WINDOWS)]
    labels = [f"W{WINDOWS - 1 - i}" for i in range(WINDOWS)]  # W4 … W0

    with Session(engine) as session:
        accounts = session.exec(
            select(Account).where(
                Account.status != "deleted_from_marzban",
                Account.deleted_at.is_(None),
            )
        ).all()
        groups = {g.id: g for g in session.exec(select(Group)).all()}
        rows = []
        for a in accounts:
            rate = a.rate_per_gb
            if rate is None and a.group_id and groups.get(a.group_id):
                rate = groups[a.group_id].rate_per_gb
            rows.append((a, rate))

    usernames = [a.marzban_username for a, _ in rows]
    periods_by_user, why = load_periods(usernames)
    if periods_by_user is None:
        print(f"!! Marzban usage log unreadable: {why}")
        return

    # Inbound tag → port, so each account's «ضریب ۲» comes from where it
    # actually lives today, not from a stored copy. Fail-open to ×1.
    tag_port: dict | None
    try:
        inbounds = await marzban_client.list_inbounds()
        tag_port = {}
        for host, lst in (inbounds or {}).items():
            for i in lst or []:
                tag_port[i.get("tag")] = str(i.get("port") or "")
    except Exception as exc:  # noqa: BLE001 — a read-only script degrades, never dies
        print(f"!! inbound list unavailable ({type(exc).__name__}: {exc}) — «ضریب ۲» NOT applied")
        tag_port = None

    doubled: dict[int, bool] = {}
    for a, _ in rows:
        if tag_port is None:
            doubled[a.id] = False
            continue
        try:
            user = await marzban_client.get_user(a.marzban_username)
            tags: list[str] = []
            for host, lst in ((user or {}).get("inbounds") or {}).items():
                tags.extend(lst or [])
            ports = {tag_port.get(t) for t in tags}
            doubled[a.id] = bool(ports & DOUBLE_PORTS)
        except Exception:  # noqa: BLE001
            doubled[a.id] = False

    sem = asyncio.Semaphore(4)
    api_errors: list[str] = []

    async def open_share(username: str, lo: datetime, hi: datetime) -> float | None:
        try:
            async with sem:
                used = await marzban_client.get_user_usage_bytes(
                    username, lo.isoformat(sep=" "), hi.isoformat(sep=" "))
        except (MarzbanUnavailable, MarzbanAuthError) as exc:
            api_errors.append(f"{username}: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001
            api_errors.append(f"{username}: {type(exc).__name__}")
            return None
        return (used or 0) / GB

    now_naive = naive(datetime.utcnow())
    gb: dict[tuple[int, int], float | None] = {}
    incomplete: set[int] = set()

    for a, _ in rows:
        plist = periods_by_user.get(a.marzban_username)
        if plist is None:
            incomplete.add(a.id)
            for w in range(WINDOWS):
                gb[(a.id, w)] = None
            continue
        for w, (lo, hi) in enumerate(windows):
            total = 0.0
            ok = True
            for p in plist:
                p_end = p.end or now_naive
                s, e = max(lo, naive(p.start)), min(hi, p_end)
                if s >= e:
                    continue
                if lo <= p.start and hi >= p_end:
                    total += p.used_bytes / GB
                elif p.end is None:
                    share = await open_share(a.marzban_username, s, e)
                    if share is None:
                        ok = False
                        break
                    total += share
                else:
                    total += _prorate(p.used_bytes, s, e, naive(p.start), p_end) / GB
            gb[(a.id, w)] = total if ok else None
            if not ok:
                incomplete.add(a.id)

    if api_errors:
        uniq = sorted(set(api_errors))[:5]
        print(f"!! {len(set(api_errors))} account-window reads failed, e.g.: {uniq} …")

    def weight(a: Account, rate) -> float:
        return (rate if rate else 1.0) * (2.0 if doubled.get(a.id) else 1.0)

    per_acc = []
    tot_gb = [0.0] * WINDOWS
    tot_wt = [0.0] * WINDOWS
    for a, rate in rows:
        vals = [gb.get((a.id, w)) for w in range(WINDOWS)]
        if any(v is None for v in vals):
            per_acc.append((a, rate, vals, None, True))
            continue
        wgb = [v * weight(a, rate) for v in vals]
        for w in range(WINDOWS):
            tot_gb[w] += vals[w]
            tot_wt[w] += wgb[w]
        per_acc.append((a, rate, vals, wgb, False))

    print()
    head = " | ".join(f"{l:>7}" for l in labels)
    print(f"{'account':<28} {'rate':>8}  x2  {head}   (GB / weighted)")
    print("-" * (28 + 12 + 4 + (10 * WINDOWS) + 18))
    for a, rate, vals, wgb, bad in sorted(per_acc, key=lambda r: -(max(v or 0 for v in r[2]))):
        if bad:
            print(f"{a.marzban_username:<28.28} {'?':>8}  {'?':>2}  {'?':>7} / {'?':<7}" * 1)
            continue
        cells = " | ".join(f"{v:>6.2f}/{wgb[i]:>6.1f}" for i, v in enumerate(vals))
        x2 = "۲" if doubled.get(a.id) else " "
        rate_s = f"{rate:g}" if rate else "—"
        print(f"{a.marzban_username:<28.28} {rate_s:>8}  {x2:>2}  {cells}")

    print("-" * (28 + 12 + 4 + (10 * WINDOWS) + 18))
    cells = " | ".join(f"{tot_gb[w]:>6.2f}/{tot_wt[w]:>6.1f}" for w in range(WINDOWS))
    print(f"{'TOTAL (complete rows only)':<28} {'':>8}    {cells}")
    print()
    print("columns: W4 W3 W2 W1 W0 (7-day windows, oldest → last 7 days).")
    print("«ضریب ۲» applies to accounts living on inbound ports " + ", ".join(sorted(DOUBLE_PORTS)) + ".")
    if incomplete:
        names = sorted(a.marzban_username for a, _ in rows if a.id in incomplete)
        print(f"excluded from totals ({len(names)}): {', '.join(names[:12])}{' …' if len(names) > 12 else ''}")

    movers = []
    for a, rate, vals, wgb, bad in per_acc:
        if bad:
            continue
        prev = sum(vals[:WINDOWS - 1]) / (WINDOWS - 1)
        movers.append((abs(vals[WINDOWS - 1] - prev), a.marzban_username, vals[WINDOWS - 1], prev))
    movers.sort(reverse=True)
    print()
    print(f"top {TOP_MOVERS} movers (last 7 days vs the 4-week average):")
    for delta, name, last, prev in movers[:TOP_MOVERS]:
        arrow = "↑" if last >= prev else "↓"
        print(f"  {arrow} {name:<28.28} last {last:7.2f} GB   avg {prev:7.2f} GB   Δ {delta:7.2f}")


if __name__ == "__main__":
    asyncio.run(main())
