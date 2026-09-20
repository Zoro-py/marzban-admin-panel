"""Family customers: the `kind` label, the default owner of a bulk batch, and
the migration that adds the column.

Plain `python tests/test_family_kind.py` from `backend/` — no pytest, same as
the rest of this suite.

Why this exists (2026-09-21): a batch created without an owner used to leave
N ownerless accounts, which sync then adopted as N separate one-account
customers («khanevadeh1» … «Khanevadeh12» on the live panel). The debt lists
then showed twelve strangers instead of one family. These checks pin the
fix: a default batch is ONE family customer, reruns reuse it, and opting out
or naming an owner still works.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="family_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import marzban_client as marzban_module  # noqa: E402
from app.auth import require_auth  # noqa: E402
from app.db import _run_lightweight_migrations, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.marzban_client import MarzbanUnavailable  # noqa: E402
from app.models import Account, AccountEvent, Customer, Group  # noqa: E402
from sqlmodel import Session, select  # noqa: E402


class FakeMarzban:
    def __init__(self, existing=None, fail_on=None):
        self.existing = list(existing or [])
        self.fail_on = fail_on or {}

    async def list_all_users(self, page_size: int = 200) -> list[dict]:
        return [{"username": u} for u in self.existing]

    async def create_user(self, payload: dict) -> dict:
        username = payload["username"]
        if username in self.fail_on:
            raise self.fail_on[username]
        return {
            "username": username, "used_traffic": 0, "lifetime_used_traffic": 0,
            "data_limit": payload.get("data_limit"), "expire": payload.get("expire"),
            "status": "active", "subscription_url": f"/sub/token_{username}",
        }


def _client(fake: FakeMarzban) -> TestClient:
    with Session(engine) as s:
        for model in (AccountEvent, Account, Group, Customer):
            for row in s.exec(select(model)).all():
                s.delete(row)
        s.commit()
    marzban_module.marzban_client.list_all_users = fake.list_all_users
    marzban_module.marzban_client.create_user = fake.create_user
    app.dependency_overrides[require_auth] = lambda: "test-admin"
    return TestClient(app)


_failures: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual == expected:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}\n          expected: {expected!r}\n          actual:   {actual!r}")
        _failures.append(label)


def _customers() -> list[Customer]:
    with Session(engine) as s:
        return list(s.exec(select(Customer)).all())


def _owners() -> list[int | None]:
    with Session(engine) as s:
        return [a.customer_id for a in s.exec(select(Account).order_by(Account.id)).all()]


def test_migration_adds_kind() -> None:
    print("\n[1] migration: an old customer table gains `kind`, existing rows stay individual")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS customer"))
        conn.execute(text(
            "CREATE TABLE customer (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, contact VARCHAR, "
            "is_group_rep BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME)"))
        conn.execute(text("INSERT INTO customer (id, name) VALUES (1, 'old-customer')"))
    _run_lightweight_migrations()
    _run_lightweight_migrations()  # idempotent: a second startup must be a no-op
    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(customer)"))}
        kind = conn.execute(text("SELECT kind FROM customer WHERE id=1")).scalar()
    check("column exists", "kind" in cols, True)
    check("existing row is individual", kind, "individual")
    with engine.begin() as conn:  # restore the real schema for the tests below
        conn.execute(text("DROP TABLE customer"))
    init_db()


def test_default_batch_is_one_family() -> None:
    print("\n[2] a batch with no owner becomes ONE family customer named after the base")
    client = _client(FakeMarzban())
    r = client.post("/api/accounts/bulk", json={"base_name": "khanevadeh", "count": 3})
    body = r.json()
    check("http 200", r.status_code, 200)
    check("created", body["created"], 3)
    custs = _customers()
    check("exactly one customer", len(custs), 1)
    check("named after base", custs[0].name, "khanevadeh")
    check("kind is family", custs[0].kind, "family")
    check("all accounts share that owner", _owners(), [custs[0].id] * 3)
    check("result reports the customer", (body["customer_id"], body["customer_name"]), (custs[0].id, "khanevadeh"))


def test_rerun_reuses_family() -> None:
    print("\n[3] extending the family (or a different-case name) reuses the customer")
    client = _client(FakeMarzban())
    client.post("/api/accounts/bulk", json={"base_name": "khanevadeh", "count": 2})
    r = client.post("/api/accounts/bulk", json={"base_name": "Khanevadeh", "count": 2, "start_index": 10})
    check("second batch created", r.json()["created"], 2)
    check("still one customer", len(_customers()), 1)
    check("four accounts, one owner", len(set(_owners())), 1)


def test_opt_out_and_explicit_owner() -> None:
    print("\n[4] unassigned=True leaves accounts ownerless; group/customer given → no family made")
    client = _client(FakeMarzban())
    client.post("/api/accounts/bulk", json={"base_name": "scratch", "count": 2, "unassigned": True})
    check("no customer created", len(_customers()), 0)
    check("accounts ownerless", _owners(), [None, None])

    client = _client(FakeMarzban())
    cid = client.post("/api/customers", json={"name": "Mr Payer"}).json()["id"]
    client.post("/api/accounts/bulk", json={"base_name": "fam", "count": 2, "customer_id": cid})
    check("explicit customer used, no extra customer", (len(_customers()), _owners()), (1, [cid, cid]))

    client = _client(FakeMarzban())
    rep = client.post("/api/customers", json={"name": "Boss"}).json()["id"]
    gid = client.post("/api/groups", json={"name": "Office", "representative_customer_id": rep}).json()["id"]
    r = client.post("/api/accounts/bulk", json={"base_name": "staff", "count": 2, "group_id": gid})
    check("group batch made no family customer", (r.status_code, len(_customers())), (200, 1))
    check("group batch accounts carry no customer", _owners(), [None, None])


def test_failed_first_item_leaves_no_empty_family() -> None:
    print("\n[5] Marzban rejects item 1 → the family customer appears with item 2, not before")
    client = _client(FakeMarzban(fail_on={"fam1": ValueError("bad inbound")}))
    r = client.post("/api/accounts/bulk", json={"base_name": "fam", "count": 2})
    check("statuses", [i["status"] for i in r.json()["items"]], ["failed", "created"])
    check("one family customer, owning the survivor", (len(_customers()), len(_owners())), (1, 1))

    client = _client(FakeMarzban(fail_on={"gone1": ValueError("x"), "gone2": ValueError("x")}))
    client.post("/api/accounts/bulk", json={"base_name": "gone", "count": 2})
    check("every item failed → no customer minted", len(_customers()), 0)


def test_kind_api() -> None:
    print("\n[6] customers API: create/patch/filter by kind, junk rejected")
    client = _client(FakeMarzban())
    a = client.post("/api/customers", json={"name": "Solo"}).json()
    b = client.post("/api/customers", json={"name": "Clan", "kind": "family"}).json()
    check("default kind", a["kind"], "individual")
    check("family kind", b["kind"], "family")
    r = client.get("/api/customers", params={"kind": "family"})
    check("filter family", [c["name"] for c in r.json()], ["Clan"])
    check("filtered rows carry kind", r.json()[0]["kind"], "family")
    check("bad filter → 400", client.get("/api/customers", params={"kind": "vip"}).status_code, 400)
    check("bad create → 422", client.post("/api/customers", json={"name": "x", "kind": "vip"}).status_code, 422)
    p = client.patch(f"/api/customers/{a['id']}", json={"kind": "family"})
    check("patch to family", (p.status_code, p.json()["kind"]), (200, "family"))


def test_sync_adopts_into_family() -> None:
    print("\n[7] sync: a new <family><n> Marzban user joins the family; others still get a personal customer")
    import asyncio
    from app import sync_job

    client = _client(FakeMarzban())
    fam_id = client.post("/api/customers", json={"name": "Khanevadeh", "kind": "family"}).json()["id"]
    plain_id = client.post("/api/customers", json={"name": "solo", "kind": "individual"}).json()["id"]

    def mu(name):
        return {"username": name, "status": "active", "used_traffic": 0, "lifetime_used_traffic": 0,
                "data_limit": 10 * 1024 ** 3, "expire": None, "online_at": None,
                "subscription_url": f"/sub/{name}", "created_at": "2026-09-20T10:00:00"}

    async def fake_fetch():
        return [mu("khanevadeh13"), mu("solo2"), mu("stranger")]

    real = sync_job._fetch_all_marzban_users
    sync_job._fetch_all_marzban_users = fake_fetch
    try:
        out = asyncio.run(sync_job._run_sync_impl())
    finally:
        sync_job._fetch_all_marzban_users = real
    check("three accounts created", out["created"], 3)
    with Session(engine) as s:
        by_user = {a.marzban_username: a.customer_id for a in s.exec(select(Account)).all()}
        names = {c.id: c.name for c in s.exec(select(Customer)).all()}
    check("khanevadeh13 joined the family", by_user["khanevadeh13"], fam_id)
    check("solo2 did NOT join the individual «solo» (only families match)", by_user["solo2"] not in (plain_id, fam_id), True)
    check("solo2 and stranger got personal customers named after them",
          (names[by_user["solo2"]], names[by_user["stranger"]]), ("solo2", "stranger"))


def test_council_findings() -> None:
    print("\n[8] council findings: sync longest-base match, warnings, null PATCH, index, single-pass lists")
    from app import sync_job
    from app.routers import accounts as accounts_router
    from app.debt_nudge_job import collect_accruing, collect_overdue

    # -- sync: longest base wins; all-digit base never matches; oldest family wins a name clash
    fam = {"fam": 1, "fam1": 2, "1": 3}
    check("fam12 -> fam1 (longest base), not fam", sync_job._family_for_username("fam12", fam), 2)
    check("fam2 -> fam", sync_job._family_for_username("fam2", fam), 1)
    check("123 must not join a family literally named 1", sync_job._family_for_username("123", fam), None)
    check("no trailing digits -> no family", sync_job._family_for_username("fam", fam), None)
    check("case-insensitive", sync_job._family_for_username("FAM7", fam), 1)

    client = _client(FakeMarzban())
    a = client.post("/api/customers", json={"name": "Alpha", "kind": "family"}).json()["id"]
    client.post("/api/customers", json={"name": "alpha", "kind": "family"})

    async def fake_fetch():
        return [{"username": "alpha1", "status": "active", "used_traffic": 0, "lifetime_used_traffic": 0,
                 "data_limit": None, "expire": None, "online_at": None, "subscription_url": "/s", "created_at": "2026-09-20T10:00:00"}]
    import asyncio
    real = sync_job._fetch_all_marzban_users
    sync_job._fetch_all_marzban_users = fake_fetch
    try:
        asyncio.run(sync_job._run_sync_impl())
    finally:
        sync_job._fetch_all_marzban_users = real
    check("name clash between two families -> the OLDEST (lowest id) wins", _owners(), [a])

    # -- bulk: family setup failing must be REPORTED, not silent
    client = _client(FakeMarzban())
    real_ensure = accounts_router._ensure_family_customer
    def boom(session, base):
        raise RuntimeError("db hiccup")
    accounts_router._ensure_family_customer = boom
    try:
        r = client.post("/api/accounts/bulk", json={"base_name": "wfam", "count": 2}).json()
    finally:
        accounts_router._ensure_family_customer = real_ensure
    check("accounts still created", r["created"], 2)
    check("but the result carries a warning naming the count", len(r["warnings"]) == 1 and "2 account(s)" in r["warnings"][0], True)
    client = _client(FakeMarzban())
    check("a normal batch has no warnings", client.post("/api/accounts/bulk", json={"base_name": "okfam", "count": 2}).json()["warnings"], [])
    check("nonexistent customer_id is still refused", client.post("/api/accounts/bulk", json={"base_name": "zz", "count": 1, "customer_id": 999}).status_code, 404)

    # -- PATCH with explicit nulls leaves NOT NULL columns alone (was a generic 400 integrity error)
    cid = client.post("/api/customers", json={"name": "Keep", "kind": "family"}).json()["id"]
    p = client.patch(f"/api/customers/{cid}", json={"kind": None, "name": None, "contact": "@x"})
    check("PATCH nulls: 200, kind/name untouched, contact set", (p.status_code, p.json()["kind"], p.json()["name"], p.json()["contact"]), (200, "family", "Keep", "@x"))

    # -- migration: the index is created even when the column pre-exists without it
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS ix_customer_kind"))
    _run_lightweight_migrations()
    with engine.begin() as conn:
        idx = [r[1] for r in conn.execute(text("PRAGMA index_list(customer)"))]
    check("ix_customer_kind exists after migrations", "ix_customer_kind" in idx, True)

    # -- one pass: accruing takes the overdue list it is given
    o = collect_overdue()
    check("collect_accruing accepts the already-computed overdue list", isinstance(collect_accruing(o), list), True)


def main() -> int:
    init_db()
    test_migration_adds_kind()
    test_default_batch_is_one_family()
    test_rerun_reuses_family()
    test_opt_out_and_explicit_owner()
    test_failed_first_item_leaves_no_empty_family()
    test_kind_api()
    test_sync_adopts_into_family()
    test_council_findings()
    print()
    if _failures:
        print(f"RESULT: {len(_failures)} FAILED: {_failures}")
        return 1
    print("RESULT: all checks OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
