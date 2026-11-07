"""Per-account renewal: a purchase can name WHICH of the customer's services it
extends (ShopOrder.target_account_id). Without a target nothing changes
(renewable_account, byte for byte). Design: docs/ZCODE_SHOPBOT_RENEW_TARGET_DESIGN.md.

Run (from backend/):  python -m tests.test_shop_renew_target
"""

from __future__ import annotations

import asyncio
import sys

from tests import test_shop as T
from tests.test_shop import (  # noqa: F401
    BOT_HEADERS, Account, BillingMode, FakeMarzban, Session, ShopOrder, ShopOrderStatus,
    ShopUser, _credit, _make_user, _reset, engine, init_db, select, wallet_balance,
)
from sqlalchemy import text

check = T.check
TG = 777


def _setup(client, fake, credit=500_000):
    """Two separate services for one customer: A (older), then B (newest — what
    renewable_account picks). Returns (shop_user_id, acct_a, acct_b)."""
    uid = _make_user(client, telegram_id=TG)
    _credit(uid, credit)
    r1 = client.post("/api/shop/bot/purchase", headers=BOT_HEADERS,
                     json={"telegram_id": TG, "data_limit_gb": 10})
    assert r1.status_code == 200, r1.text
    intent = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                         json={"telegram_id": TG, "data_limit_gb": 10, "force_new_service": True}).json()
    r2 = client.post(f"/api/shop/bot/orders/{intent['order_id']}/pay", headers=BOT_HEADERS,
                     json={"telegram_id": TG})
    assert r2.status_code == 200, r2.text
    with Session(engine) as s:
        accts = s.exec(select(Account).order_by(Account.id)).all()
        assert len(accts) == 2, len(accts)
        return uid, accts[0].id, accts[1].id


def _gb(fake, username):
    return round(fake.panel[username]["data_limit"] / 1024 ** 3, 2)


def _username(acct_id):
    with Session(engine) as s:
        return s.get(Account, acct_id).marzban_username


def test_migration_column_exists() -> None:
    print("\n[R1] shoporder.target_account_id exists, nullable")
    _reset(FakeMarzban())
    with engine.connect() as conn:
        cols = {r[1]: r for r in conn.execute(text("PRAGMA table_info(shoporder)"))}
    check("column present", "target_account_id" in cols, True)
    check("nullable (notnull=0)", cols["target_account_id"][3], 0)


def test_target_extends_that_account_only() -> None:
    print("\n[R2] a target extends THAT account, not renewable_account")
    fake = FakeMarzban(); client = _reset(fake)
    uid, a, b = _setup(client, fake)
    ua, ub = _username(a), _username(b)
    before_a, before_b = _gb(fake, ua), _gb(fake, ub)
    intent = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                         json={"telegram_id": TG, "data_limit_gb": 20, "target_account_id": a})
    check("order created", intent.status_code, 200)
    intent = intent.json()
    check("creates_new is False for a targeted renewal", intent["creates_new"], False)
    check("target_label names the service", bool(intent.get("target_label")), True)
    r = client.post(f"/api/shop/bot/orders/{intent['order_id']}/pay", headers=BOT_HEADERS,
                    json={"telegram_id": TG})
    check("paid", r.status_code, 200)
    check("A grew by 20 GB", _gb(fake, ua), round(before_a + 20, 2))
    check("B untouched", _gb(fake, ub), before_b)
    with Session(engine) as s:
        o = s.get(ShopOrder, intent["order_id"])
        check("target frozen on the order", o.target_account_id, a)
        check("order extends A", o.extends_account_id, a)
        check("no third account minted", len(s.exec(select(Account)).all()), 2)


def test_no_target_is_todays_behaviour() -> None:
    print("\n[R3] without a target: renewable_account (parity)")
    fake = FakeMarzban(); client = _reset(fake)
    uid, a, b = _setup(client, fake)
    ub = _username(b); before_b = _gb(fake, ub)
    intent = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                         json={"telegram_id": TG, "data_limit_gb": 10}).json()
    client.post(f"/api/shop/bot/orders/{intent['order_id']}/pay", headers=BOT_HEADERS, json={"telegram_id": TG})
    check("newest service B grew", _gb(fake, ub), round(before_b + 10, 2))


def test_someone_elses_account_is_404() -> None:
    print("\n[R4] a target that is not yours: 404, no order, no money")
    fake = FakeMarzban(); client = _reset(fake)
    uid, a, b = _setup(client, fake)
    other = _make_user(client, telegram_id=888)
    _credit(other, 200_000)
    client.post("/api/shop/bot/purchase", headers=BOT_HEADERS, json={"telegram_id": 888, "data_limit_gb": 10})
    with Session(engine) as s:
        theirs = s.exec(select(ShopOrder).where(ShopOrder.shop_user_id == other)).first().account_id
        n_orders = len(s.exec(select(ShopOrder)).all())
        bal = wallet_balance(s, uid)
    r = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                    json={"telegram_id": TG, "data_limit_gb": 10, "target_account_id": theirs})
    check("404", r.status_code, 404)
    r = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                    json={"telegram_id": TG, "data_limit_gb": 10, "target_account_id": 999999})
    check("nonexistent id: the same 404", r.status_code, 404)
    with Session(engine) as s:
        check("no order row created", len(s.exec(select(ShopOrder)).all()), n_orders)
        check("wallet untouched", wallet_balance(s, uid), bal)


def test_force_new_plus_target_is_400() -> None:
    print("\n[R5] force_new_service + target = contradictory")
    fake = FakeMarzban(); client = _reset(fake)
    uid, a, b = _setup(client, fake)
    r = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                    json={"telegram_id": TG, "data_limit_gb": 10, "force_new_service": True,
                          "target_account_id": a})
    check("400", r.status_code, 400)


def test_per_target_guards() -> None:
    print("\n[R6] guards judge the TARGET: payg / unlimited / disabled")
    fake = FakeMarzban(); client = _reset(fake)
    uid, a, b = _setup(client, fake)
    with Session(engine) as s:   # A -> unlimited, B stays normal
        acc = s.get(Account, a); acc.data_limit = 0; s.add(acc); s.commit()
    r = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                    json={"telegram_id": TG, "data_limit_gb": 10, "target_account_id": a})
    check("unlimited target: 409", r.status_code, 409)
    check("...SERVICE_IS_UNLIMITED prefix", r.json()["detail"].startswith("SERVICE_IS_UNLIMITED"), True)
    with Session(engine) as s:   # A -> payg
        acc = s.get(Account, a); acc.data_limit = 10 * 1024 ** 3; acc.billing_mode = BillingMode.payg
        s.add(acc); s.commit()
    r = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                    json={"telegram_id": TG, "data_limit_gb": 10, "target_account_id": a})
    check("payg target: 409 PAYG_TOPUP_INSTEAD", (r.status_code, r.json()["detail"].startswith("PAYG_TOPUP_INSTEAD")), (409, True))
    with Session(engine) as s:
        acc = s.get(Account, a); acc.billing_mode = BillingMode.prepay; acc.status = "disabled"
        s.add(acc); s.commit()
    r = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                    json={"telegram_id": TG, "data_limit_gb": 10, "target_account_id": a})
    check("disabled target: refused", r.status_code in (400, 409), True)
    # the guard looks at THE TARGET: B (healthy) is still renewable by id
    r = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                    json={"telegram_id": TG, "data_limit_gb": 10, "target_account_id": b})
    check("healthy sibling still accepted", r.status_code, 200)


def test_pay_reverifies_the_target_before_any_debit() -> None:
    print("\n[R7] money line: pay re-checks ownership, zero debit on failure")
    fake = FakeMarzban(); client = _reset(fake)
    uid, a, b = _setup(client, fake)
    other = _make_user(client, telegram_id=888)
    _credit(other, 200_000)
    client.post("/api/shop/bot/purchase", headers=BOT_HEADERS, json={"telegram_id": 888, "data_limit_gb": 10})
    with Session(engine) as s:
        theirs = s.exec(select(ShopOrder).where(ShopOrder.shop_user_id == other)).first().account_id
    intent = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                         json={"telegram_id": TG, "data_limit_gb": 10, "target_account_id": a}).json()
    with Session(engine) as s:   # the target is swapped to someone else's account
        o = s.get(ShopOrder, intent["order_id"]); o.target_account_id = theirs
        s.add(o); s.commit()
        bal = wallet_balance(s, uid)
    created_before = len(fake.created)
    r = client.post(f"/api/shop/bot/orders/{intent['order_id']}/pay", headers=BOT_HEADERS, json={"telegram_id": TG})
    check("refused", r.status_code in (400, 404), True)
    with Session(engine) as s:
        check("zero money moved", wallet_balance(s, uid), bal)
        check("order still awaiting", s.get(ShopOrder, intent["order_id"]).status, ShopOrderStatus.awaiting_payment)
    check("nothing created on the panel", len(fake.created), created_before)


def test_target_gone_from_panel_refunds_never_mints_new() -> None:
    print("\n[R8] target vanished from the panel after the order: refund, no new account")
    fake = FakeMarzban(); client = _reset(fake)
    uid, a, b = _setup(client, fake)
    ua = _username(a)
    intent = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                         json={"telegram_id": TG, "data_limit_gb": 10, "target_account_id": a}).json()
    with Session(engine) as s:
        bal = wallet_balance(s, uid)
    del fake.panel[ua]
    created_before = len(fake.created)
    client.post(f"/api/shop/bot/orders/{intent['order_id']}/pay", headers=BOT_HEADERS, json={"telegram_id": TG})
    with Session(engine) as s:
        o = s.get(ShopOrder, intent["order_id"])
        check("order failed (refunded)", o.status, ShopOrderStatus.failed)
        check("wallet back to where it was", wallet_balance(s, uid), bal)
    check("NO replacement account minted", len(fake.created), created_before)


def test_session_bit_and_row_flags() -> None:
    print("\n[R9] session.per_account_renewal + accounts[].can_renew")
    fake = FakeMarzban(); client = _reset(fake)
    uid, a, b = _setup(client, fake)
    s_ = client.post("/api/shop/bot/session", headers=BOT_HEADERS, json={"telegram_id": TG}).json()
    check("per_account_renewal True", s_.get("per_account_renewal"), True)
    rows = client.get("/api/shop/bot/accounts", headers=BOT_HEADERS, params={"telegram_id": TG}).json()
    check("both cards can renew", sorted(r["can_renew"] for r in rows), [True, True])
    with Session(engine) as s:
        acc = s.get(Account, a); acc.data_limit = 0; s.add(acc); s.commit()
    rows = client.get("/api/shop/bot/accounts", headers=BOT_HEADERS, params={"telegram_id": TG}).json()
    by = {r["account_id"]: r["can_renew"] for r in rows}
    check("unlimited card: can_renew False", by[a], False)
    check("normal card: can_renew True", by[b], True)


def main() -> int:
    init_db()
    for test in (
        test_migration_column_exists,
        test_target_extends_that_account_only,
        test_no_target_is_todays_behaviour,
        test_someone_elses_account_is_404,
        test_force_new_plus_target_is_400,
        test_per_target_guards,
        test_pay_reverifies_the_target_before_any_debit,
        test_target_gone_from_panel_refunds_never_mints_new,
        test_session_bit_and_row_flags,
    ):
        test()
    if T._failures:
        print(f"\n{len(T._failures)} FAILED: {T._failures}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
