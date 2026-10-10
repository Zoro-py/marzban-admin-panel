"""GET /api/reports/anomalies — cross-account billing anomalies (app/audit.py).

Plain `python -m tests.test_audit` from `backend/` (same harness shape as
tests/test_statement.py). Each of the four checks gets a case that SHOULD
flag and a case that must NOT — the two no_matching_bill/rate_mismatch false
positives caught testing this against the live DB are exactly why every
negative case here is as deliberate as the positive one.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows consoles default stdout to the system codepage (cp1252 etc.), which
# crashes on the first Persian character a finding's detail carries (e.g. a
# dampened_resize note quoted verbatim from sync_job) — reconfigure to UTF-8
# before anything prints.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_TMP_DB = Path(tempfile.mkdtemp(prefix="audit_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
_MZ_DB = Path(tempfile.mkdtemp(prefix="audit_mz_")) / "db.sqlite3"
os.environ["MARZBAN_DB_PATH"] = str(_MZ_DB)
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
for _k in ("BOT_TOKEN", "BOT_ADMIN_CHAT_ID", "SHOP_BOT_TOKEN", "SHOP_BOT_API_KEY", "DELEGATE_BOT_API_KEY"):
    os.environ[_k] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, func, select  # noqa: E402

from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account, AccountEvent, AppSettings, BillingMode, Customer, LedgerEntry, LedgerSource, LedgerType, RateChange,
)

init_db()
failures: list[str] = []


def check(label: str, cond: bool, detail: object = "") -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        failures.append(label)


GB = 1024 ** 3
app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)


def audit(**params):
    r = client.get("/api/reports/anomalies", params=params)
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


def by_kind(result, kind):
    return [f for f in result["findings"] if f["kind"] == kind]


def has_account(rows, name):
    return any(f["account"] == name for f in rows)


now = datetime.now(timezone.utc).replace(tzinfo=None)
with Session(engine) as s:
    s.add(AppSettings(id=1, default_rate_per_gb=5000))
    cust = Customer(name="Audit Cust")
    s.add(cust)
    s.commit()
    s.refresh(cust)

    accounts = {
        # --- rate_mismatch: history around the ONE real false positive found live ---
        # Rate went 5000(default) -> 3000 at t_rate_change. A charge BEFORE that
        # change, billed at the then-default 5000, must NOT be flagged even
        # though the account's CURRENT rate is 3000 (the exact live bug).
        "au_rate_ok": Account(marzban_username="au_rate_ok", customer_id=cust.id, billing_mode=BillingMode.prepay,
                              rate_per_gb=3000, data_limit=50 * GB, billed_data_limit=50 * GB, used_traffic=1 * GB),
        # A charge AFTER the rate change that does NOT match the new rate —
        # a genuine drift, must be flagged.
        "au_rate_bad": Account(marzban_username="au_rate_bad", customer_id=cust.id, billing_mode=BillingMode.prepay,
                               rate_per_gb=3000, data_limit=50 * GB, billed_data_limit=50 * GB, used_traffic=1 * GB),
        # --- external_change_unbilled ---
        "au_ext_unbilled": Account(marzban_username="au_ext_unbilled", customer_id=cust.id, billing_mode=BillingMode.prepay,
                                   data_limit=50 * GB, billed_data_limit=50 * GB, used_traffic=1 * GB),
        "au_ext_billed": Account(marzban_username="au_ext_billed", customer_id=cust.id, billing_mode=BillingMode.prepay,
                                 data_limit=50 * GB, billed_data_limit=50 * GB, used_traffic=1 * GB),
        # payg: an external data_limit change here is a cap move, not a sale —
        # must be excluded even though nothing was ever charged for it.
        "au_payg_ext": Account(marzban_username="au_payg_ext", customer_id=cust.id, billing_mode=BillingMode.payg,
                               data_limit=300 * GB, billed_data_limit=300 * GB, used_traffic=4 * GB, usage_baseline=0),
        # --- stale_open_period ---
        "au_stale": Account(marzban_username="au_stale", customer_id=cust.id, billing_mode=BillingMode.prepay,
                            data_limit=50 * GB, billed_data_limit=10 * GB, used_traffic=1 * GB, auto_renew_enabled=True),
        "au_fresh": Account(marzban_username="au_fresh", customer_id=cust.id, billing_mode=BillingMode.prepay,
                            data_limit=50 * GB, billed_data_limit=10 * GB, used_traffic=1 * GB),
        # Open just as long as au_stale, but fully billed (pending == 0) —
        # a long-open but already-settled package is not an anomaly.
        "au_stale_settled": Account(marzban_username="au_stale_settled", customer_id=cust.id, billing_mode=BillingMode.prepay,
                                    data_limit=50 * GB, billed_data_limit=50 * GB, used_traffic=1 * GB),
        # --- dampened_resize ---
        "au_dampened": Account(marzban_username="au_dampened", customer_id=cust.id, billing_mode=BillingMode.prepay,
                               data_limit=20 * GB, billed_data_limit=20 * GB, used_traffic=1 * GB),
        "au_normal_resize": Account(marzban_username="au_normal_resize", customer_id=cust.id, billing_mode=BillingMode.prepay,
                                    data_limit=20 * GB, billed_data_limit=20 * GB, used_traffic=1 * GB),
    }
    s.add_all(accounts.values())
    s.commit()
    for a in accounts.values():
        s.refresh(a)
    ids = {name: a.id for name, a in accounts.items()}

    t_rate_change = now - timedelta(days=20)
    s.add(RateChange(scope="account", account_id=ids["au_rate_ok"], old_rate=None, new_rate=3000, created_at=t_rate_change))
    s.add(RateChange(scope="account", account_id=ids["au_rate_bad"], old_rate=None, new_rate=3000, created_at=t_rate_change))

    s.add(LedgerEntry(type=LedgerType.charge, amount=50000, gb_amount=10, account_id=ids["au_rate_ok"], customer_id=cust.id,
                      date=t_rate_change - timedelta(days=5), note="billed at the default, before the rate change"))
    s.add(LedgerEntry(type=LedgerType.charge, amount=50000, gb_amount=10, account_id=ids["au_rate_bad"], customer_id=cust.id,
                      date=t_rate_change + timedelta(days=5), note="should be 30000 at the new 3000 rate, is not"))

    t_ext = now - timedelta(days=10)
    s.add(AccountEvent(account_id=ids["au_ext_unbilled"], action="external_data_limit_increase",
                       detail="+40.00 GB added outside this dashboard (in Marzban directly)", date=t_ext, source=LedgerSource.sync))
    s.add(AccountEvent(account_id=ids["au_ext_billed"], action="external_data_limit_increase",
                       detail="+40.00 GB added outside this dashboard (in Marzban directly)", date=t_ext, source=LedgerSource.sync))
    s.add(LedgerEntry(type=LedgerType.charge, amount=200000, account_id=ids["au_ext_billed"], customer_id=cust.id,
                      date=t_ext + timedelta(hours=2), note="covers the external top-up"))
    s.add(AccountEvent(account_id=ids["au_payg_ext"], action="external_data_limit_increase",
                       detail="+270.00 GB added outside this dashboard (in Marzban directly)", date=t_ext, source=LedgerSource.sync))

    s.add(AccountEvent(account_id=ids["au_dampened"], action="next_plan_auto_queued",
                       detail="Auto-queued 20 GB / 31 days (1.00 گیگ مونده, monthly avg 40 GB; dampened from raw 40 GB over 6 observed days)",
                       date=now - timedelta(days=2), source=LedgerSource.sync))
    s.add(AccountEvent(account_id=ids["au_normal_resize"], action="next_plan_auto_queued",
                       detail="Auto-queued 20 GB / 31 days (1.00 گیگ مونده, monthly avg 18 GB)",
                       date=now - timedelta(days=2), source=LedgerSource.sync))
    s.commit()

con = sqlite3.connect(_MZ_DB)
con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, created_at DATETIME, used_traffic BIGINT)")
con.execute("CREATE TABLE user_usage_logs (id INTEGER PRIMARY KEY, user_id INT, used_traffic_at_reset BIGINT, reset_at DATETIME)")
# Open period started 60 days ago: stale.
con.execute("INSERT INTO users VALUES (1, 'au_stale', ?, ?)", (str(now - timedelta(days=60)), 1 * GB))
# Open period started 5 days ago: fresh, same unbilled shape otherwise.
con.execute("INSERT INTO users VALUES (2, 'au_fresh', ?, ?)", (str(now - timedelta(days=5)), 1 * GB))
# Open 90 days, but billed_data_limit == data_limit -> fully settled already.
con.execute("INSERT INTO users VALUES (3, 'au_stale_settled', ?, ?)", (str(now - timedelta(days=90)), 1 * GB))
con.commit()
con.close()

ledger_before = None
with Session(engine) as s:
    ledger_before = s.exec(select(func.count()).select_from(LedgerEntry)).one()

r = audit(lookback_days=60)

# ---- 1) rate_mismatch ----------------------------------------------------------
rm = by_kind(r, "rate_mismatch")
check("rate_mismatch: a charge billed at the default BEFORE a later rate change is not flagged",
      not has_account(rm, "au_rate_ok"), rm)
check("rate_mismatch: a charge that doesn't match the rate in effect when it was posted IS flagged",
      has_account(rm, "au_rate_bad"), rm)
bad = next(f for f in rm if f["account"] == "au_rate_bad")
check("rate_mismatch: detail carries the rate that should have applied and the gap",
      bad["detail"]["rate_then"] == 3000 and abs(bad["detail"]["gap"] - (50000 - 30000)) < 1, bad["detail"])

# ---- 2) external_change_unbilled -------------------------------------------------
ext = by_kind(r, "external_change_unbilled")
check("external_change_unbilled: a direct-Marzban change with nothing posted near it is flagged",
      has_account(ext, "au_ext_unbilled"), ext)
check("external_change_unbilled: the same change WITH a charge posted 2h later is not flagged",
      not has_account(ext, "au_ext_billed"), ext)
check("external_change_unbilled: a PAYG account's data_limit change is excluded (cap move, not a sale)",
      not has_account(ext, "au_payg_ext"), ext)

# ---- 3) stale_open_period --------------------------------------------------------
stale = by_kind(r, "stale_open_period")
check("stale_open_period: a package open 60 days with real pending debt is flagged",
      has_account(stale, "au_stale"), stale)
check("stale_open_period: a package open only 5 days is not flagged even with the same pending shape",
      not has_account(stale, "au_fresh"), stale)
check("stale_open_period: a package open 90 days but fully billed (pending=0) is not flagged",
      not has_account(stale, "au_stale_settled"), stale)

# ---- 4) dampened_resize -----------------------------------------------------------
damp = by_kind(r, "dampened_resize")
check("dampened_resize: an auto-queue event the sizing job itself capped is flagged",
      has_account(damp, "au_dampened"), damp)
check("dampened_resize: a normal (non-capped) auto-queue event is not flagged",
      not has_account(damp, "au_normal_resize"), damp)

# ---- 5) shape / errors -------------------------------------------------------------
check("counts matches findings length per kind",
      sum(r["counts"].values()) == len(r["findings"]), (r["counts"], len(r["findings"])))
check("findings sorted newest-first", all(r["findings"][i]["date"] >= r["findings"][i + 1]["date"] for i in range(len(r["findings"]) - 1)))
check("usage_log_available true with the fake Marzban DB mounted", r["usage_log_available"] is True, r["usage_log_error"])
check("errors: lookback_days out of range -> 400",
      client.get("/api/reports/anomalies", params={"lookback_days": 0}).status_code == 400)
check("errors: lookback_days out of range (too large) -> 400",
      client.get("/api/reports/anomalies", params={"lookback_days": 366}).status_code == 400)

# ---- 6) read-only -------------------------------------------------------------------
with Session(engine) as s:
    ledger_after = s.exec(select(func.count()).select_from(LedgerEntry)).one()
check("read-only: no ledger row written by any anomalies call", ledger_after == ledger_before, (ledger_before, ledger_after))

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("All audit cases passed.")
