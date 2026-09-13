"""End-to-end checks for the bulk ("family") account flow.

Plain `python -m tests.test_bulk_accounts` from `backend/`, no pytest — same
reasoning as db.py's hand-written migrations: this project is small enough
that one more dependency costs more than it buys, and a test that needs a
framework installed is a test nobody runs before deploying.

What it actually covers is the part that can't be checked by reading: the
per-item commit policy (AGENTS.md §4.5). Marzban's create endpoint is faked
so a failure can be injected at a chosen item, and the assertions then check
that the items BEFORE it are still in the database — which is exactly what a
single loop-wrapping transaction would silently get wrong.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Must be set before app.config is imported — the engine is built at import
# time from this value, so a later override would point at the real DB.
_TMP_DB = Path(tempfile.mkdtemp(prefix="bulk_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
# Blank on purpose: with no bot configured, the endpoint must still create
# every account and simply report notifications_queued=False. A test that
# configured a bot would try to reach Telegram for real.
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import marzban_client as marzban_module  # noqa: E402
from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, AccountEvent  # noqa: E402
from app.marzban_client import MarzbanUnavailable  # noqa: E402
from sqlmodel import Session, select  # noqa: E402


class FakeMarzban:
    """Stands in for a live panel. `fail_on` maps a username to the exception
    raised instead of creating it, so a failure can be placed at any position
    in the batch."""

    def __init__(self, existing: list[str] | None = None, fail_on: dict | None = None):
        self.existing = list(existing or [])
        self.fail_on = fail_on or {}
        self.created: list[str] = []

    async def list_all_users(self, page_size: int = 200) -> list[dict]:
        return [{"username": u} for u in self.existing]

    async def create_user(self, payload: dict) -> dict:
        username = payload["username"]
        if username in self.fail_on:
            raise self.fail_on[username]
        self.created.append(username)
        return {
            "username": username,
            "used_traffic": 0,
            "lifetime_used_traffic": 0,
            "data_limit": payload.get("data_limit"),
            "expire": payload.get("expire"),
            "status": payload.get("status", "active"),
            "subscription_url": f"/sub/token_{username}",
        }


def _fresh_client(fake: FakeMarzban) -> TestClient:
    """Deliberately NOT used as a context manager by the callers below.

    Entering TestClient runs the app's lifespan, which starts the APScheduler
    — fine once, but the second entry tries to start it again against an event
    loop the first exit already closed, and every test after the first dies
    with "Event loop is closed". These tests exercise HTTP handlers, not the
    scheduler, so init_db() in main() is the only piece of startup they need.
    """
    _reset_db()
    marzban_module.marzban_client.list_all_users = fake.list_all_users
    marzban_module.marzban_client.create_user = fake.create_user
    app.dependency_overrides[require_auth] = lambda: "test-admin"
    return TestClient(app)


def _reset_db() -> None:
    """Every test starts from an empty local DB.

    Without this the tests are order-dependent in the worst way: accounts
    created by an earlier test are real rows, so they land in the NEXT test's
    `taken` set and change which usernames it plans. That made test [3] report
    a collision on a name its own fixture had declared free — a failure of the
    harness that reads exactly like a failure of the code.
    """
    with Session(engine) as session:
        for model in (AccountEvent, Account):
            for row in session.exec(select(model)).all():
                session.delete(row)
        session.commit()


def _tracked_usernames() -> list[str]:
    with Session(engine) as session:
        return sorted(session.exec(select(Account.marzban_username)).all())


_failures: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual == expected:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}\n          expected: {expected!r}\n          actual:   {actual!r}")
        _failures.append(label)


def test_happy_path() -> None:
    print("\n[1] plain batch of 3 on an empty panel")
    fake = FakeMarzban()
    client = _fresh_client(fake)
    r = client.post("/api/accounts/bulk", json={"base_name": "khanevade", "count": 3,
                                                "data_limit_gb": 30, "expire_days": 30})
    check("http 200", r.status_code, 200)
    body = r.json()
    check("created count", body["created"], 3)
    check("start index", body["start_index"], 1)
    check("names", [i["marzban_username"] for i in body["items"]],
          ["khanevade1", "khanevade2", "khanevade3"])
    check("relative sub url resolved against panel host",
          body["items"][0]["subscription_url"], "https://panel.test/sub/token_khanevade1")
    check("no bot configured -> not queued", body["notifications_queued"], False)
    check("all tracked locally", _tracked_usernames(),
          ["khanevade1", "khanevade2", "khanevade3"])


def test_continues_from_highest() -> None:
    print("\n[2] a second batch continues the series instead of restarting")
    fake = FakeMarzban(existing=["khanevade1", "khanevade2", "khanevade3"])
    client = _fresh_client(fake)
    r = client.post("/api/accounts/bulk", json={"base_name": "khanevade", "count": 2})
    body = r.json()
    check("start index", body["start_index"], 4)
    check("names", [i["marzban_username"] for i in body["items"]], ["khanevade4", "khanevade5"])


def test_explicit_start_reports_collisions() -> None:
    print("\n[3] explicit start_index keeps its numbers and reports the taken one")
    fake = FakeMarzban(existing=["khanevade4"])
    client = _fresh_client(fake)
    r = client.post("/api/accounts/bulk",
                    json={"base_name": "khanevade", "count": 3, "start_index": 4})
    body = r.json()
    check("statuses", [i["status"] for i in body["items"]],
          ["skipped_exists", "created", "created"])
    check("numbers did not shift", [i["marzban_username"] for i in body["items"]],
          ["khanevade4", "khanevade5", "khanevade6"])
    check("skipped counted", body["skipped"], 1)
    check("created counted", body["created"], 2)


def test_per_item_failure_keeps_earlier_items() -> None:
    print("\n[4] a 4xx on item 2 must not undo item 1 (AGENTS.md §4.5)")
    fake = FakeMarzban(fail_on={"fam2": ValueError("Marzban create_user failed (409): exists")})
    client = _fresh_client(fake)
    r = client.post("/api/accounts/bulk", json={"base_name": "fam", "count": 3})
    body = r.json()
    check("statuses", [i["status"] for i in body["items"]], ["created", "failed", "created"])
    check("batch continued past the failure", body["created"], 2)
    check("earlier item survived in the DB", "fam1" in _tracked_usernames(), True)
    check("later item survived too", "fam3" in _tracked_usernames(), True)
    check("failed item not tracked", "fam2" in _tracked_usernames(), False)


def test_panel_down_aborts_rest() -> None:
    print("\n[5] panel going down mid-batch stops the rest and says why")
    fake = FakeMarzban(fail_on={"grp2": MarzbanUnavailable("connection refused")})
    client = _fresh_client(fake)
    r = client.post("/api/accounts/bulk", json={"base_name": "grp", "count": 4})
    body = r.json()
    check("statuses", [i["status"] for i in body["items"]],
          ["created", "failed", "failed", "failed"])
    check("aborted_reason set", "connection refused" in (body["aborted_reason"] or ""), True)
    check("no further Marzban calls after the abort", fake.created, ["grp1"])
    check("item 1 still really created", "grp1" in _tracked_usernames(), True)


def test_preview_creates_nothing() -> None:
    print("\n[6] preview shows the names without touching Marzban")
    fake = FakeMarzban(existing=["pv1"])
    client = _fresh_client(fake)
    r = client.post("/api/accounts/bulk/preview",
                    json={"base_name": "pv", "count": 2, "start_index": 1})
    body = r.json()
    check("http 200", r.status_code, 200)
    check("names", [n["marzban_username"] for n in body["names"]], ["pv1", "pv2"])
    check("collision flagged", [n["already_exists"] for n in body["names"]], [True, False])
    check("will_create", body["will_create"], 1)
    check("nothing created", fake.created, [])


def test_audit_event_written() -> None:
    print("\n[7] every created account gets its creation event")
    fake = FakeMarzban()
    client = _fresh_client(fake)
    client.post("/api/accounts/bulk", json={"base_name": "audit", "count": 2})
    with Session(engine) as session:
        accounts = session.exec(select(Account).where(Account.marzban_username.like("audit%"))).all()
        events = session.exec(select(AccountEvent)).all()
        ids_with_events = {e.account_id for e in events if e.action == "create"}
    check("both accounts created", len(accounts), 2)
    check("both have a create event", all(a.id in ids_with_events for a in accounts), True)


def test_bad_input_rejected() -> None:
    print("\n[8] input that would produce unusable usernames is refused up front")
    fake = FakeMarzban()
    client = _fresh_client(fake)
    # 28 chars + a 5-digit index = 33, one over Marzban's limit. The SAME base
    # with a 2-digit index fits and is allowed — the guard is about the batch's
    # highest index, not the base name on its own.
    too_long = client.post("/api/accounts/bulk",
                           json={"base_name": "a" * 28, "count": 2, "start_index": 99999})
    bad_chars = client.post("/api/accounts/bulk", json={"base_name": "kh an", "count": 2})
    too_many = client.post("/api/accounts/bulk", json={"base_name": "kh", "count": 500})
    check("over-long base rejected (400)", too_long.status_code, 400)
    check("nothing created for it", fake.created, [])
    check("invalid characters rejected (422)", bad_chars.status_code, 422)
    check("over-cap count rejected (422)", too_many.status_code, 422)


def main() -> int:
    init_db()
    for test in (
        test_happy_path,
        test_continues_from_highest,
        test_explicit_start_reports_collisions,
        test_per_item_failure_keeps_earlier_items,
        test_panel_down_aborts_rest,
        test_preview_creates_nothing,
        test_audit_event_written,
        test_bad_input_rejected,
    ):
        test()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {_failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
