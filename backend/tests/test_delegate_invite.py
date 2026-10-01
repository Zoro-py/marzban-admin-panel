"""Invite-link onboarding (2026-10-01): /delegate_invite minting a one-time
t.me deep link per customer/group, delegate_bot claiming it via
POST /api/delegate/bot/claim, and DELETE only touching still-pending rows.
Runs through the real FastAPI app (TestClient, same harness as
tests/test_delegate_smoke.py). No Marzban calls happen on any of these
paths — the money path (create/renew/delete accounts) is not exercised here.

Plain `python -m tests.test_delegate_invite` from `backend/`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="delegate_invite_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""
os.environ["SHOP_BOT_API_KEY"] = ""
os.environ["DELEGATE_BOT_API_KEY"] = "test-delegate-key"
# The username invite links are built from — read by Settings at import time.
os.environ["DELEGATE_BOT_USERNAME"] = "test_delegate_bot"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.auth import require_auth  # noqa: E402
from app.config import settings as app_settings  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Customer, Delegate, Group  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)
BOT_HEADERS = {"X-Delegate-Bot-Key": "test-delegate-key"}

with Session(engine) as session:
    customer = Customer(name="Invite Customer")
    other = Customer(name="Other Invite Customer")
    third = Customer(name="Third Invite Customer")
    group_rep = Customer(name="Group Rep")
    session.add_all([customer, other, third, group_rep])
    session.commit()
    session.refresh(customer)
    session.refresh(other)
    session.refresh(third)
    session.refresh(group_rep)
    group = Group(name="Invite Group", representative_customer_id=group_rep.id)
    session.add(group)
    session.commit()
    session.refresh(group)
    customer_id, other_id, third_id, group_id = customer.id, other.id, third.id, group.id


def token_of(invite_url: str) -> str:
    assert "?start=dlgtok_" in invite_url, invite_url
    return invite_url.split("?start=dlgtok_", 1)[1]


# ── happy path: mint → still locked out → claim → session works ─────────
r = client.post("/api/delegate/invite", json={"customer_id": customer_id})
check("invite created (200)", r.status_code == 200)
invite = r.json()
check("invite_url points at the configured delegate bot username",
      invite["invite_url"] == f"https://t.me/test_delegate_bot?start=dlgtok_{token_of(invite['invite_url'])}")
check("pending row: telegram_id None, is_active False", invite["telegram_id"] is None and invite["is_active"] is False)
check("claim_expires_at is set on the pending row", bool(invite["claim_expires_at"]))
check("scope_name reflects the customer", invite["scope_name"] == "Invite Customer")

r = client.get("/api/delegate")
listed = next(d for d in r.json() if d["id"] == invite["id"])
check("operator list shows the pending invite with its invite_url",
      listed["invite_url"] == invite["invite_url"] and listed["telegram_id"] is None)

r = client.post("/api/delegate/bot/session", headers=BOT_HEADERS, json={"telegram_id": 7001})
check("before the claim, /session still refuses that telegram_id (403)", r.status_code == 403)

token = token_of(invite["invite_url"])
r = client.post("/api/delegate/bot/claim", headers=BOT_HEADERS,
                json={"token": token, "telegram_id": 7001, "telegram_username": "ali_t"})
check("claim succeeds (200)", r.status_code == 200)
claimed = r.json()
check("claim returns the same shape /session returns",
      set(claimed) == {"delegate_id", "label", "scope_name", "default_duration_days", "quick_volumes_gb"})
check("claim returns the right scope", claimed["scope_name"] == "Invite Customer")
check("label taken from telegram_username when the row had none", claimed["label"] == "ali_t")

r = client.post("/api/delegate/bot/session", headers=BOT_HEADERS, json={"telegram_id": 7001})
check("after the claim, /session works for that telegram_id", r.status_code == 200)

r = client.get("/api/delegate")
listed = next(d for d in r.json() if d["id"] == invite["id"])
check("claimed row no longer exposes invite_url/claim_expires_at",
      listed["invite_url"] is None and listed["claim_expires_at"] is None and listed["telegram_id"] == 7001)

# ── token is single-use: reuse after a successful claim → 404 ────────────
r = client.post("/api/delegate/bot/claim", headers=BOT_HEADERS,
                json={"token": token, "telegram_id": 7999})
check("reusing a consumed token is rejected (404)", r.status_code == 404)
with Session(engine) as session:
    row = session.get(Delegate, invite["id"])
    check("the claimed row's token was cleared, not left dangling",
          row.claim_token is None and row.claim_expires_at is None)

# ── duplicate pending invite for the same scope → 409 ────────────────────
# (the customer_id invite above was already CLAIMED further up, so its scope
# is free again — the duplicate rule is exercised on other_id, which holds a
# live pending invite right now.)
r = client.post("/api/delegate/invite", json={"customer_id": other_id})
check("a different customer can still get an invite (200)", r.status_code == 200)
other_invite = r.json()
r = client.post("/api/delegate/invite", json={"customer_id": other_id})
check("second invite for the same customer while one is pending → 409", r.status_code == 409)

# ── validation: XOR scope + existence, same rules as create_or_update ────
r = client.post("/api/delegate/invite", json={"customer_id": other_id, "group_id": group_id})
check("both scopes at once rejected (400)", r.status_code == 400)
r = client.post("/api/delegate/invite", json={})
check("neither scope rejected (400)", r.status_code == 400)
r = client.post("/api/delegate/invite", json={"customer_id": 999999})
check("unknown customer_id rejected (404)", r.status_code == 404)

# ── group invite + claim ─────────────────────────────────────────────────
r = client.post("/api/delegate/invite", json={"group_id": group_id})
check("group invite created (200)", r.status_code == 200 and r.json()["scope_name"] == "Invite Group")
group_token = token_of(r.json()["invite_url"])
r = client.post("/api/delegate/bot/claim", headers=BOT_HEADERS,
                json={"token": group_token, "telegram_id": 7002})
check("group invite claim succeeds", r.status_code == 200 and r.json()["scope_name"] == "Invite Group")

# ── telegram_id collision → 409 (some other row already holds this id) ──
# third_id still has no pending invite here (other_id's is kept pending for
# the DELETE test below), so this POST genuinely mints one.
r = client.post("/api/delegate/invite", json={"customer_id": third_id})
check("invite for the third customer created (200)", r.status_code == 200)
collision_token = token_of(r.json()["invite_url"])
r = client.post("/api/delegate/bot/claim", headers=BOT_HEADERS,
                json={"token": collision_token, "telegram_id": 7001})
check("claiming with an already-linked telegram_id is rejected (409)", r.status_code == 409)
with Session(engine) as session:
    row = session.exec(select(Delegate).where(Delegate.claim_token == collision_token)).first()
    check("the refused claim did NOT consume the token (row still pending)",
          row is not None and row.telegram_id is None and row.is_active is False)

# ── expired token → 404 ──────────────────────────────────────────────────
from datetime import datetime, timezone  # noqa: E402

with Session(engine) as session:
    expired = Delegate(customer_id=other_id, telegram_id=None, is_active=False,
                       claim_token="expired-token",
                       claim_expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    session.add(expired)
    session.commit()
r = client.post("/api/delegate/bot/claim", headers=BOT_HEADERS,
                json={"token": "expired-token", "telegram_id": 7003})
check("an expired token is rejected (404)", r.status_code == 404)
r = client.post("/api/delegate/bot/claim", headers=BOT_HEADERS,
                json={"token": "no-such-token", "telegram_id": 7003})
check("an unknown token is rejected (404)", r.status_code == 404)

# ── DELETE: pending only ─────────────────────────────────────────────────
r = client.delete(f"/api/delegate/{invite['id']}")
check("DELETE on a CLAIMED delegate is refused (409)", r.status_code == 409)
r = client.delete(f"/api/delegate/{other_invite['id']}")
check("DELETE on a pending invite works (200)", r.status_code == 200)
r = client.delete(f"/api/delegate/{other_invite['id']}")
check("DELETE on an already-deleted invite is 404", r.status_code == 404)

# ── 503 when the delegate bot username isn't configured ─────────────────
saved_username = app_settings.delegate_bot_username
app_settings.delegate_bot_username = ""
r = client.post("/api/delegate/invite", json={"customer_id": other_id})
app_settings.delegate_bot_username = saved_username
check("invite creation without a configured username is refused (503)", r.status_code == 503)

# ── SQLite unique semantics after the nullable flip: many NULLs, one 7001 ─
with Session(engine) as session:
    pendings = session.exec(select(Delegate).where(Delegate.telegram_id.is_(None))).all()
    check("multiple pending rows (telegram_id NULL) coexist under the unique index",
          len(pendings) >= 2)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All delegate invite-link cases passed.")
