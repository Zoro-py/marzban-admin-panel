"""The operator-side DIRECT Telegram bind: POST /api/shop/link/{id}/telegram
mirrors the invite+claim flow's writes and refusals without a token —
the operator types the id, the bot then knows which accounts belong to
which Telegram user.

Locked here: happy bind (+ linked-account count), idempotent same-pair
rebind, both 409 collision directions, group-rep refusal, payg-customer
refusal, pending-invite token superseded, DM failure reported NOT failed,
and unlink still working after a direct bind.

Run from backend/: venv/Scripts/python.exe tests/test_shop_direct_telegram_bind.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="shop_direct_bind_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""  # unset: every DM raises -> notification_delivered False
os.environ["SHOP_BOT_API_KEY"] = "test-shop-key"
os.environ["SHOP_BOT_USERNAME"] = "testshopbot"  # the invite-mint gate reads this

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Customer, ShopUser, utcnow  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


client = TestClient(app)


def _auth_header() -> dict:
    from app.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token('test')}"}


AUTH = _auth_header()


def reset_db() -> None:
    from app.models import ShopSettings

    with Session(engine) as session:
        for model in (ShopUser, Customer, ShopSettings):
            for row in session.exec(select(model)).all():
                session.delete(row)
        session.commit()
        session.add(ShopSettings(id=1, is_open=True, price_per_gb=3000, min_gb=5, max_gb=200,
                                 plan_duration_days=30, card_number="6037-XXXX", card_holder="Operator",
                                 username_prefix="shop", min_topup=10000, max_topup=50_000_000))
        session.commit()


def make_customer(name: str, *, group_rep: bool = False, payg: bool = False) -> int:
    from app.models import Account, BillingMode

    with Session(engine) as session:
        c = Customer(name=name, is_group_rep=group_rep)
        session.add(c)
        session.commit()
        session.refresh(c)
        if payg:
            session.add(Account(marzban_username=f"{name.lower().replace(' ', '_')}_a",
                                customer_id=c.id, billing_mode=BillingMode.payg, status="active"))
            session.commit()
        return c.id


def bind(customer_id: int, telegram_id: int):
    return client.post(f"/api/shop/link/{customer_id}/telegram", headers=AUTH,
                       json={"telegram_id": telegram_id, "telegram_username": f"user{telegram_id}"})


# ---- 1. happy bind: creates the ShopUser, sets the live link, counts accounts,
# reports the DM as NOT delivered (SHOP_BOT_TOKEN unset here) with a note. ----
reset_db()
cid = make_customer("Ali Rezaei")
with Session(engine) as session:
    from app.models import Account

    session.add(Account(marzban_username="ali_a", customer_id=cid, status="active"))
    session.add(Account(marzban_username="ali_b", customer_id=cid, status="active"))
    session.commit()

r = bind(cid, 111_222_333)
check("bind returns 200", r.status_code == 200)
d = r.json()
check("linked telegram echoed", d.get("linked_telegram_id") == 111_222_333)
check("both of the customer's accounts linked", d.get("accounts_linked") == 2)
check("DM failure is REPORTED, not failed", d.get("notification_delivered") is False
      and "SHOP_BOT_TOKEN" in (d.get("notification_note") or ""))
with Session(engine) as session:
    cust = session.get(Customer, cid)
    su = session.get(ShopUser, cust.shop_user_id)
    check("Customer.shop_user_id set (the live link)", cust.shop_user_id is not None)
    check("ShopUser created for that telegram", su.telegram_id == 111_222_333)
    check("pending invite fields cleared", cust.shop_link_token is None and cust.shop_link_expires_at is None)

# ---- 2. idempotent same-pair bind ----
r = bind(cid, 111_222_333)
check("same-pair rebind is 200 (idempotent)", r.status_code == 200)
with Session(engine) as session:
    check("still exactly one ShopUser", len(session.exec(select(ShopUser)).all()) == 1)

# ---- 3. another customer binding the SAME telegram -> allowed (owner 2026-10-26:
# one person may hold a whole family's services); then unlinked again so section 4's
# premise (cid2 free) still holds ----
cid2 = make_customer("Sara Ahmadi")
r = bind(cid2, 111_222_333)
check("telegram already on another customer -> 200 (multi-link)", r.status_code == 200)
with Session(engine) as session:
    check("both customers now share that telegram", len([c for c in session.exec(select(Customer)).all() if c.shop_user_id is not None and c.id in (cid, cid2)]) == 2)
client.delete(f"/api/shop/link/{cid2}")

# ---- 4. customer already bound -> binding a DIFFERENT telegram -> 409 with unlink hint ----
r = bind(cid2, 999_888_777)
r2 = bind(cid, 999_888_777)
check("customer already linked elsewhere -> 409", r2.status_code == 409)
check("409 carries the unlink hint", "DELETE /api/shop/link" in r2.json().get("detail", ""))

# ---- 5. group representative refused ----
cid_rep = make_customer("Family Head", group_rep=True)
r = bind(cid_rep, 444_555_666)
check("group representative -> 409", r.status_code == 409)
check("refusal names the delegate alternative", "دلیگیت" in r.json().get("detail", ""))

# ---- 6. payg-billing customer refused (economy mixing) ----
cid_payg = make_customer("Payg Person", payg=True)
r = bind(cid_payg, 444_555_666)
check("payg-billing customer links like any other (2026-10-26: one balance, no conversion)", r.status_code == 200)

# ---- 7. a pending invite is superseded by a direct bind ----
cid3 = make_customer("With Pending")
r = client.post("/api/shop/link-invite", headers=AUTH, json={"customer_id": cid3})
check("invite minted for the pending case", r.status_code == 200)
r = bind(cid3, 777_000_111)
check("direct bind over a pending invite works", r.status_code == 200)
with Session(engine) as session:
    cust = session.get(Customer, cid3)
    check("pending token died with the bind", cust.shop_link_token is None)

# ---- 8. unlink still works after a direct bind, and a rebind to the same
# telegram afterwards is a clean second bind (not a residue collision) ----
r = client.delete(f"/api/shop/link/{cid3}", headers=AUTH)
check("unlink after direct bind -> 200", r.status_code == 200)
r = bind(cid3, 777_000_111)
check("re-bind after unlink works", r.status_code == 200)
with Session(engine) as session:
    cust = session.get(Customer, cid3)
    check("link live again", cust.shop_user_id is not None)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("all direct telegram bind cases passed.")
