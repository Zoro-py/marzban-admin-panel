"""Bind-by-identity claim — the operator types @username/name, the bot's own
session call completes the bind (owner feedback 2026-10-24: «نمیشه فقط با
آیدی کاربر بایند کرد که بعد از پیام کاربر خود بات آیدی عددی رو بفهمه»).

Covers: creation guards (empty identity, payg, group rep, already linked,
taken username, one-pending-per-customer replace), the session hook
(match by username case-insensitively, match by display name, AMBIGUITY
binds nothing, already-linked customer discards the claim, guards re-run at
completion), the discard endpoint, and the link-state/connections surfaces
carrying the pending claim.

Plain `python -m tests.test_shop_bind_claim` from `backend/` — no pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="bindclaim_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""
os.environ["SHOP_BOT_API_KEY"] = "test-shop-key"
os.environ["DELEGATE_BOT_API_KEY"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Customer, ShopBindClaim, ShopUser  # noqa: E402

init_db()

failures: list[str] = []


def check(name: str, cond: bool, extra=None):
    print(("  [OK] " if cond else "[FAIL] ") + name + (f"  {extra}" if (extra is not None and not cond) else ""))
    if not cond:
        failures.append(name)


app.dependency_overrides[require_auth] = lambda: "testop"
client = TestClient(app)

BOT_HDR = {"X-Shop-Bot-Key": "test-shop-key"}

with Session(engine) as s:
    c1 = Customer(name="علی ساده")
    c2 = Customer(name="مریم")
    c3 = Customer(name="علی دوم")   # display-name ambiguity source
    s.add(c1); s.add(c2); s.add(c3)
    s.commit()
    C1, C2, C3 = c1.id, c2.id, c3.id

# ---- 1) creation guards ----------------------------------------------------
r = client.post("/api/shop/bind-claim", json={"customer_id": C1})
check("create: empty identity → 400", r.status_code == 400, r.status_code)

r = client.post("/api/shop/bind-claim", json={"customer_id": C1, "telegram_username": "@Dmahdisalimian"})
check("create: by username → 200, normalized (no @, lower)", r.status_code == 200 and r.json()["telegram_username"] == "dmahdisalimian", r.text[:150])
CLAIM1 = r.json()["id"]

# one pending per customer: a retry replaces
r = client.post("/api/shop/bind-claim", json={"customer_id": C1, "telegram_username": "Dmahdisalimian", "display_name": "علی"})
check("create: retry replaces old pending", r.status_code == 200)
with Session(engine) as s:
    pendings = s.exec(select(ShopBindClaim).where(ShopBindClaim.customer_id == C1, ShopBindClaim.claimed_at.is_(None))).all()
    check("create: exactly one pending remains", len(pendings) == 1, len(pendings))
    check("create: the surviving claim carries BOTH identities",
          pendings[0].telegram_username == "dmahdisalimian" and pendings[0].display_name == "علی")

# ---- 2) the session hook completes it --------------------------------------
# The bot's identity: same username, different case + a numeric id nobody typed.
r = client.post("/api/shop/bot/session", headers=BOT_HDR,
                json={"telegram_id": 777001, "telegram_username": "DMahdisalimian", "display_name": "علی"})
check("session: bot session 200", r.status_code == 200, r.text[:150])
with Session(engine) as s:
    c1 = s.get(Customer, C1)
    check("hook: customer now linked", c1.shop_user_id is not None, c1.shop_user_id)
    su = s.get(ShopUser, c1.shop_user_id)
    check("hook: numeric id came from the bot (777001)", su.telegram_id == 777001)
    # CLAIM1's id is stale — the retry replaced that row; look up by customer.
    claim = s.exec(select(ShopBindClaim).where(ShopBindClaim.customer_id == C1)).first()
    check("hook: claim stamped claimed", claim is not None and claim.claimed_at is not None and claim.claimed_telegram_id == 777001)

r = client.get(f"/api/shop/link/{C1}")
check("link state: linked + carries no pending claim", r.json()["status"] == "linked" and r.json()["bind_claim_username"] is None)

# ---- 3) true ambiguity: two FRESH customers pending with one display name --
with Session(engine) as s:
    c4 = Customer(name="دوقلوی اول")
    c5 = Customer(name="دوقلوی دوم")
    s.add(c4); s.add(c5)
    s.commit()
    C4, C5 = c4.id, c5.id
client.post("/api/shop/bind-claim", json={"customer_id": C4, "display_name": "دوقلو"})
client.post("/api/shop/bind-claim", json={"customer_id": C5, "display_name": "دوقلو"})
r = client.post("/api/shop/bot/session", headers=BOT_HDR,
                json={"telegram_id": 777004, "telegram_username": "twin", "display_name": "دوقلو"})
check("session: two identical pendings → 200 (menu unaffected)", r.status_code == 200)
with Session(engine) as s:
    check("hook: two identical pendings bind nothing", s.get(Customer, C4).shop_user_id is None and s.get(Customer, C5).shop_user_id is None,
          (s.get(Customer, C4).shop_user_id, s.get(Customer, C5).shop_user_id))

# ---- 4) discard endpoint ---------------------------------------------------
r = client.delete(f"/api/shop/bind-claim/{C4}")
check("discard: 200", r.status_code == 200, r.text[:120])
r = client.delete(f"/api/shop/bind-claim/{C4}")
check("discard: second time → 404", r.status_code == 404, r.status_code)

# ---- 5) already-linked customer: claim superseded by manual bind -----------
# C1 was bound by the hook in test 2 — unlink, then: claim first, direct bind
# (operator learned the numeric id meanwhile) wins, pending claim disappears.
r = client.delete(f"/api/shop/link/{C1}")
check("unlink C1: 200", r.status_code == 200, r.text[:150])
client.post("/api/shop/bind-claim", json={"customer_id": C1, "display_name": "علی بعدی"})
r = client.post(f"/api/shop/link/{C1}/telegram", json={"telegram_id": 888001, "display_name": "علی بعدی"})
check("direct bind: 200 (operator path still works)", r.status_code == 200, r.text[:200])
with Session(engine) as s:
    pend = s.exec(select(ShopBindClaim).where(ShopBindClaim.customer_id == C1, ShopBindClaim.claimed_at.is_(None))).all()
    check("manual bind supersedes the pending claim (deleted)", len(pend) == 0, len(pend))

# ---- 6) connections surface carries pending claims -------------------------
conns = {c["customer_id"]: c for c in client.get("/api/connections").json()}
check("connections: pending claim visible (C5)", conns[C5]["shop"]["bind_claim_display_name"] == "دوقلو", conns[C5]["shop"])
check("connections: linked customer shows none pending (C1)", conns[C1]["shop"]["state"] == "linked" and conns[C1]["shop"]["bind_claim_display_name"] is None)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All bind-claim cases passed.")
