"""Shop-link for EXISTING customers (2026-10-01): the operator mints a
one-time t.me deep link per hand-created customer (POST /api/shop/link-invite),
the shop bot claims it (POST /api/shop/bot/claim-link), and from then on the
customer's operator-created accounts appear in GET /api/shop/bot/accounts
(source "linked") and a purchase would renew their real account in place
(shop_service.renewable_account's linked fallback). Runs through the real
FastAPI app (TestClient, same harness as tests/test_delegate_invite.py). No
Marzban calls happen on any of these paths — the purchase/extend money path
itself is not exercised here, only the identity binding and the read paths.

Plain `python -m tests.test_shop_link` from `backend/`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="shop_link_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""
# The key shopbot presents on /api/shop/bot/* and the username deep links are
# built from — both read by Settings at import time.
os.environ["SHOP_BOT_API_KEY"] = "test-shop-key"
os.environ["SHOP_BOT_USERNAME"] = "test_shop_bot"
os.environ["DELEGATE_BOT_API_KEY"] = ""
os.environ["DELEGATE_BOT_USERNAME"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db, _run_lightweight_migrations  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Customer, ShopOrder, ShopOrderStatus, ShopUser  # noqa: E402
from app.shop_service import is_existing_customer, renewable_account  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)
BOT_HEADERS = {"X-Shop-Bot-Key": "test-shop-key"}


def _make_account(session: Session, *, customer_id: int, username: str, **kw) -> Account:
    account = Account(marzban_username=username, customer_id=customer_id, **kw)
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


with Session(engine) as session:
    alice = Customer(name="Link Alice")
    payg_man = Customer(name="Payg Pete")
    group_rep = Customer(name="Group Rep Rita", is_group_rep=True)
    session.add_all([alice, payg_man, group_rep])
    session.commit()
    for c in (alice, payg_man, group_rep):
        session.refresh(c)
    # Pete's own account bills pay-as-you-go — the link must refuse him.
    _make_account(session, customer_id=payg_man.id, username="pete_payg", billing_mode="payg")
    # Alice: two operator-created accounts (one later disabled — still VISIBLE
    # in the bot, as disabled) and no shop history at all.
    alice_a = _make_account(session, customer_id=alice.id, username="alice_a", data_limit=30 * 1024**3,
                            used_traffic=5 * 1024**3, status="active")
    _make_account(session, customer_id=alice.id, username="alice_b", status="disabled")
    # Plain ints — the ORM objects expire when this session closes.
    alice_id, payg_id, rep_id = alice.id, payg_man.id, group_rep.id
    alice_a_id = alice_a.id

# ── invite: eligibility gates ─────────────────────────────────────────────
r = client.post("/api/shop/link-invite", json={"customer_id": 99999})
check("invite unknown customer → 404", r.status_code == 404)

r = client.post("/api/shop/link-invite", json={"customer_id": rep_id})
check("group rep refused → 409", r.status_code == 409 and "group" in r.json()["detail"].lower())

r = client.post("/api/shop/link-invite", json={"customer_id": payg_id})
check("payg customer refused → 409", r.status_code == 409 and "pay" in r.json()["detail"].lower())

r = client.post("/api/shop/link-invite", json={"customer_id": alice_id})
check("invite alice → 200", r.status_code == 200)
invite = r.json()
check("invite_url carries the bot username + prefix",
      invite["invite_url"].startswith("https://t.me/test_shop_bot?start=shoplnk_"))
token = invite["invite_url"].split("shoplnk_", 1)[1]

r = client.post("/api/shop/link-invite", json={"customer_id": alice.id})
check("second pending invite refused → 409", r.status_code == 409 and "pending" in r.json()["detail"].lower())

r = client.get(f"/api/shop/link/{alice_id}")
check("link state reads pending with the SAME url", r.status_code == 200 and r.json()["status"] == "pending"
      and r.json()["invite_url"] == invite["invite_url"])

# ── claim ─────────────────────────────────────────────────────────────────
r = client.post("/api/shop/bot/claim-link", json={"token": "no-such-token", "telegram_id": 777001}, headers=BOT_HEADERS)
check("claim unknown token → 404", r.status_code == 404)

r = client.post("/api/shop/bot/claim-link", json={"token": token, "telegram_id": 777001}, headers=BOT_HEADERS)
check("claim alice's token → 200", r.status_code == 200)
claimed = r.json()
check("claim names the customer and counts BOTH live accounts",
      claimed["customer_name"] == "Link Alice" and claimed["accounts_linked"] == 2)

r = client.post("/api/shop/bot/claim-link", json={"token": token, "telegram_id": 777002}, headers=BOT_HEADERS)
check("token reuse after claim → 404 (cleared)", r.status_code == 404)

# ── listing merges linked accounts ────────────────────────────────────────
r = client.get("/api/shop/bot/accounts", params={"telegram_id": 777001}, headers=BOT_HEADERS)
rows = r.json()
check("linked customer's accounts appear in the bot listing", r.status_code == 200 and len(rows) == 2)
sources = {row["source"] for row in rows}
check("all rows are source=linked, order_id null",
      sources == {"linked"} and all(row["order_id"] is None for row in rows))
usernames = {row["marzban_username"] for row in rows}
check("disabled account still shows, both carry live usage fields",
      usernames == {"alice_a", "alice_b"} and all("used_traffic" in row for row in rows))

# ── dedupe: the same account reachable via shop order AND link ────────────
with Session(engine) as session:
    shop_user = session.exec(ShopUser.__table__.select().where(ShopUser.telegram_id == 777001)).first()
    order = ShopOrder(shop_user_id=shop_user.id, data_limit_gb=30, duration_days=30, price=1000,
                      status=ShopOrderStatus.delivered, account_id=alice_a_id, marzban_username="alice_a")
    session.add(order)
    session.commit()
r = client.get("/api/shop/bot/accounts", params={"telegram_id": 777001}, headers=BOT_HEADERS)
rows = r.json()
alice_a_rows = [row for row in rows if row["marzban_username"] == "alice_a"]
check("account reachable both ways appears ONCE, as the shop row",
      len(alice_a_rows) == 1 and alice_a_rows[0]["source"] == "shop" and alice_a_rows[0]["order_id"] is not None)

# ── unclaimed-telegram collision ──────────────────────────────────────────
with Session(engine) as session:
    # A second customer with a live invite.
    bob = Customer(name="Link Bob")
    session.add(bob)
    session.commit()
    session.refresh(bob)
    bob_id = bob.id
r = client.post("/api/shop/link-invite", json={"customer_id": bob_id}).json()
bob_token = r["invite_url"].split("shoplnk_", 1)[1]
# The telegram that already carries Alice must not take Bob too.
r = client.post("/api/shop/bot/claim-link", json={"token": bob_token, "telegram_id": 777001}, headers=BOT_HEADERS)
check("telegram already bound to alice refuses bob → 409", r.status_code == 409 and "another customer" in r.json()["detail"])

# ── renewal primitives on the linked customer ─────────────────────────────
with Session(engine) as session:
    acct = renewable_account(session, shop_user.id)
    check("renewable_account falls back to the linked customer's newest live account",
          acct is not None and acct.marzban_username == "alice_a")
    check("is_existing_customer is True from the link alone (no trial)",
          is_existing_customer(session, shop_user.id) is True)

# ── unlink: the operator's off-switch ─────────────────────────────────────
r = client.delete(f"/api/shop/link/{alice.id}")
check("unlink → ok", r.status_code == 200)
r = client.get("/api/shop/bot/accounts", params={"telegram_id": 777001}, headers=BOT_HEADERS)
rows = [row for row in r.json() if row["source"] == "linked"]
check("linked rows gone after unlink (shop order row remains)", len(rows) == 0)
r = client.delete(f"/api/shop/link/{alice.id}")
check("unlink again → 404 (nothing linked, nothing pending)", r.status_code == 404)

# ── expired invite + discard ──────────────────────────────────────────────
from datetime import timedelta  # noqa: E402
from app.models import utcnow  # noqa: E402

with Session(engine) as session:
    stale = Customer(name="Stale Link", shop_link_token="stale-token", shop_link_expires_at=utcnow() - timedelta(days=1))
    session.add(stale)
    session.commit()
r = client.post("/api/shop/bot/claim-link", json={"token": "stale-token", "telegram_id": 777003}, headers=BOT_HEADERS)
check("expired token → 404", r.status_code == 404)

with Session(engine) as session:
    carol = Customer(name="Link Carol")
    session.add(carol)
    session.commit()
    session.refresh(carol)
    carol_id = carol.id
carol_invite = client.post("/api/shop/link-invite", json={"customer_id": carol_id}).json()
r = client.delete(f"/api/shop/link-invite/{carol_id}")
check("discard pending → ok", r.status_code == 200)
r = client.post("/api/shop/bot/claim-link",
                json={"token": carol_invite["invite_url"].split("shoplnk_", 1)[1], "telegram_id": 777004},
                headers=BOT_HEADERS)
check("discarded token → 404", r.status_code == 404)
r = client.delete(f"/api/shop/link-invite/{carol_id}")
check("discard with nothing pending → 404", r.status_code == 404)

# ── migration idempotency: a second full pass must be a clean no-op ───────
try:
    _run_lightweight_migrations()
    check("lightweight migrations re-run cleanly (idempotent)", True)
except Exception as exc:  # noqa: BLE001
    check(f"lightweight migrations re-run cleanly (idempotent) — raised {exc!r}", False)

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("test_shop_link: all checks passed")
