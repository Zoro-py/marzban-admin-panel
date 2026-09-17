"""Unit checks for services.monthly_avg_usage's cycle-paced estimator: the
figure must reflect what the account is doing NOW (current cycle pace) once
the cycle has half a day of observation, falling back to the lifetime
average only when the cycle is unobserved/too fresh/idle — the exact blind
spot the operator hit (user burns ~2GB in a day, panel kept showing their
idle-era 2.7 GB/mo, auto-queue sized the next plan at a fraction of demand).

Plain `python -m tests.test_monthly_avg` from `backend/` — no DB, no Marzban;
pure in-memory Account instances.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import Account  # noqa: E402
from app.services import GB, MIN_CYCLE_PACE_DAYS, monthly_avg_usage  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


NOW = datetime(2026, 9, 18, 12, 0, 0)
GBx = GB


def acct(**kwargs) -> Account:
    base = dict(
        marzban_username="avg-test",
        used_traffic=0,
        lifetime_used_traffic=0,
        first_seen_traffic=0,
        usage_baseline=0,
    )
    base.update(kwargs)
    return Account(**base)


# ── the operator's exact case: idle history, then a 2GB burst yesterday ──
burst = acct(
    used_traffic=int(1.87 * GB),
    lifetime_used_traffic=int(1.89 * GBx),
    first_seen_traffic=0,
    first_seen_traffic_at=NOW - timedelta(days=21),
    usage_baseline_at=NOW - timedelta(hours=20),   # current cycle started yesterday
)
avg, conf, days = monthly_avg_usage(burst, NOW)
check("burst case: cycle pace wins over stale lifetime average", avg == 67.32)  # 1.87GB over 20h, * 30
check("burst case: preliminary confidence (cycle < 30d)", conf == "preliminary")
check("burst case: observed_days is the cycle window", abs(days - 20 / 24) < 1e-9)

# ── fresh cycle (< MIN_CYCLE_PACE_DAYS): lifetime fallback, not noise ──
fresh = acct(
    used_traffic=int(0.4 * GB),
    lifetime_used_traffic=int(2.0 * GBx),
    first_seen_traffic=int(1.2 * GBx),
    first_seen_traffic_at=NOW - timedelta(days=10),
    usage_baseline_at=NOW - timedelta(hours=2),   # 2h old cycle
)
avg, conf, days = monthly_avg_usage(fresh, NOW)
check("fresh cycle: falls back to lifetime average", avg == 2.4)  # 0.8GB over 10d * 30
check("fresh cycle: window reported is the lifetime one", abs(days - 10.0) < 1e-9)

# ── idle this cycle (0 bytes): lifetime fallback, not a lying "0 GB/mo" ──
idle = acct(
    used_traffic=0,                                   # genuinely nothing this cycle
    lifetime_used_traffic=int(6.5 * GBx),
    first_seen_traffic=int(1.5 * GBx),
    first_seen_traffic_at=NOW - timedelta(days=40),
    usage_baseline_at=NOW - timedelta(days=5),    # cycle 5 days old, nothing used
)
avg, conf, days = monthly_avg_usage(idle, NOW)
check("idle cycle: lifetime average shown (0 GB/mo would read as 'dead')", avg == 3.75)

# ── legacy row without usage_baseline_at: lifetime path still works ──
legacy = acct(
    lifetime_used_traffic=int(30 * GBx),
    first_seen_traffic=int(10 * GBx),
    first_seen_traffic_at=NOW - timedelta(days=20),
    usage_baseline_at=None,
)
avg, conf, days = monthly_avg_usage(legacy, NOW)
check("legacy (no cycle anchor): lifetime average", avg == 30.0)

# ── brand-new account: genuinely insufficient data, no guess ──
brand_new = acct(
    first_seen_traffic_at=NOW - timedelta(days=1),
    usage_baseline_at=NOW - timedelta(hours=1),
)
avg, conf, days = monthly_avg_usage(brand_new, NOW)
check("brand-new account: insufficient_data, not a guess", avg is None and conf == "insufficient_data")

# ── a calm user whose cycle pace is BELOW their lifetime average ──
calm = acct(
    used_traffic=int(10 * GB),
    lifetime_used_traffic=int(100 * GBx),
    first_seen_traffic=int(20 * GBx),
    first_seen_traffic_at=NOW - timedelta(days=30),
    usage_baseline_at=NOW - timedelta(days=10),   # 10GB over 10d = 30 GB/mo
)
avg, conf, days = monthly_avg_usage(calm, NOW)
check("calm user: current 30 GB/mo pace wins over 80 GB/mo lifetime", avg == 30.0)

# ── a full month of cycle observation: full confidence ──
settled = acct(
    used_traffic=int(60 * GB),
    lifetime_used_traffic=int(160 * GBx),
    first_seen_traffic=int(20 * GBx),
    first_seen_traffic_at=NOW - timedelta(days=60),
    usage_baseline_at=NOW - timedelta(days=32),   # 60GB over 32d
)
avg, conf, days = monthly_avg_usage(settled, NOW)
check("full month cycle: full confidence", conf == "full" and avg == 56.25)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All monthly-average cases passed.")
