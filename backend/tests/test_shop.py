"""End-to-end checks for the self-serve shop's money paths.

Plain `python -m tests.test_shop` from `backend/`, no pytest — same reasoning
as tests/test_bulk_accounts.py.

These cover the things that cannot be checked by reading the code:

  - the AUTH BOUNDARY actually holds (the shop bot's key does not open the
    operator's endpoints, and the operator's JWT is genuinely required)
  - a wallet cannot be spent twice by two concurrent purchases
  - a failed provision really refunds, and a stranded order is swept
  - approving the same receipt twice credits once

Every one of those is a way to lose or invent real money, and every one of
them looks fine when you read the code.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="shop_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""
# The key under test. Set before app.config is imported, like DATABASE_URL.
os.environ["SHOP_BOT_API_KEY"] = "test-shop-key"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import marzban_client as marzban_module  # noqa: E402
from app.auth import require_auth  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    AccountEvent,
    ShopOrder,
    ShopOrderStatus,
    ShopSettings,
    ShopTopup,
    ShopUser,
    ShopWalletEntry,
    ShopWalletEntryType,
    utcnow,
)
from app.shop_service import (  # noqa: E402
    STUCK_ORDER_TIMEOUT_MINUTES,
    _purchase_locks,
    post_wallet_entry,
    purchase,
    sweep_stuck_orders,
    wallet_balance,
)

BOT_HEADERS = {"X-Shop-Bot-Key": "test-shop-key"}


class FakeMarzban:
    def __init__(self, fail_with: Exception | None = None):
        self.fail_with = fail_with
        self.created: list[str] = []

    async def create_user(self, payload: dict) -> dict:
        if self.fail_with is not None:
            raise self.fail_with
        username = payload["username"]
        self.created.append(username)
        return {
            "username": username,
            "used_traffic": 0,
            "lifetime_used_traffic": 0,
            "data_limit": payload.get("data_limit"),
            "expire": payload.get("expire"),
            "status": "active",
            "subscription_url": f"/sub/tok_{username}",
        }


_failures: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual == expected:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}\n          expected: {expected!r}\n          actual:   {actual!r}")
        _failures.append(label)


def _reset(fake: FakeMarzban | None = None, *, open_shop: bool = True) -> TestClient:
    """Fresh DB and a fresh shop config for every test — see
    tests/test_bulk_accounts.py's _reset_db for why shared state between tests
    produces failures that read like code bugs."""
    with Session(engine) as session:
        for model in (ShopWalletEntry, ShopTopup, ShopOrder, ShopUser, AccountEvent, Account, ShopSettings):
            for row in session.exec(select(model)).all():
                session.delete(row)
        session.commit()
        session.add(ShopSettings(
            id=1, is_open=open_shop, price_per_gb=3000, min_gb=5, max_gb=200,
            plan_duration_days=30, card_number="6037-XXXX", card_holder="Operator",
            username_prefix="shop", min_topup=10000, max_topup=50_000_000,
        ))
        session.commit()
    _purchase_locks.clear()
    if fake is not None:
        marzban_module.marzban_client.create_user = fake.create_user
    app.dependency_overrides[require_auth] = lambda: "test-admin"
    return TestClient(app)


def _make_user(client: TestClient, telegram_id: int = 555) -> int:
    r = client.post("/api/shop/bot/session", headers=BOT_HEADERS,
                    json={"telegram_id": telegram_id, "display_name": "Ali"})
    return r.json()["shop_user_id"]


def _credit(shop_user_id: int, amount: int) -> None:
    with Session(engine) as session:
        post_wallet_entry(session, shop_user_id, entry_type=ShopWalletEntryType.topup,
                          amount=amount, note="test credit")


def test_auth_boundary() -> None:
    print("\n[1] the shop bot's key opens the bot endpoints and nothing else")
    fake = FakeMarzban()
    client = _reset(fake)

    no_key = client.post("/api/shop/bot/session", json={"telegram_id": 1})
    bad_key = client.post("/api/shop/bot/session", headers={"X-Shop-Bot-Key": "wrong"},
                          json={"telegram_id": 1})
    good_key = client.post("/api/shop/bot/session", headers=BOT_HEADERS, json={"telegram_id": 1})
    check("no key -> 401", no_key.status_code, 401)
    check("wrong key -> 401", bad_key.status_code, 401)
    check("right key -> 200", good_key.status_code, 200)

    # The operator's endpoints must NOT accept the bot key. require_auth is
    # overridden for these tests, so drop the override to test it honestly.
    app.dependency_overrides.pop(require_auth, None)
    admin_with_bot_key = client.get("/api/shop/users", headers=BOT_HEADERS)
    admin_no_auth = client.get("/api/shop/users")
    check("operator endpoint rejects the bot key", admin_with_bot_key.status_code, 401)
    check("operator endpoint rejects no auth", admin_no_auth.status_code, 401)
    app.dependency_overrides[require_auth] = lambda: "test-admin"


def test_purchase_happy_path() -> None:
    print("\n[2] a funded wallet buys, is debited exactly once, and gets a link")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 100_000)

    r = client.post("/api/shop/bot/purchase", headers=BOT_HEADERS,
                    json={"telegram_id": 555, "data_limit_gb": 10})
    check("http 200", r.status_code, 200)
    body = r.json()
    check("price = 10 GB x 3000", body["price"], 30_000)
    check("balance after", body["balance"], 70_000)
    check("account created in Marzban", fake.created, [body["marzban_username"]])
    check("relative sub url resolved", body["subscription_url"],
          f"https://panel.test/sub/tok_{body['marzban_username']}")
    with Session(engine) as session:
        entries = session.exec(select(ShopWalletEntry).where(ShopWalletEntry.shop_user_id == uid)).all()
        debits = [e for e in entries if e.type == ShopWalletEntryType.purchase]
    check("exactly one debit", len(debits), 1)
    check("debit is negative", debits[0].amount, -30_000)


def test_insufficient_balance() -> None:
    print("\n[3] an underfunded wallet buys nothing and is not touched")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 10_000)

    r = client.post("/api/shop/bot/purchase", headers=BOT_HEADERS,
                    json={"telegram_id": 555, "data_limit_gb": 10})
    check("rejected (400)", r.status_code, 400)
    check("nothing created", fake.created, [])
    with Session(engine) as session:
        check("balance untouched", wallet_balance(session, uid), 10_000)
        check("no order row", len(session.exec(select(ShopOrder)).all()), 0)


def test_no_double_spend() -> None:
    print("\n[4] two concurrent purchases cannot both spend the same balance")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    # Enough for exactly ONE 10 GB plan (30,000 T), not two.
    _credit(uid, 35_000)

    async def run_both():
        # Two coroutines racing on one wallet, which is what two taps on the
        # bot's buy button a few milliseconds apart actually produce.
        def one():
            with Session(engine) as session:
                user = session.exec(select(ShopUser).where(ShopUser.id == uid)).one()
                return purchase(session, user, 10)
        return await asyncio.gather(one(), one(), return_exceptions=True)

    results = asyncio.run(run_both())
    ok = [r for r in results if isinstance(r, ShopOrder)]
    errs = [r for r in results if isinstance(r, Exception)]
    check("exactly one purchase succeeded", len(ok), 1)
    check("the other was refused", len(errs), 1)
    with Session(engine) as session:
        check("balance debited once", wallet_balance(session, uid), 5_000)
        check("never went negative", wallet_balance(session, uid) >= 0, True)
    check("only one Marzban account created", len(fake.created), 1)


def test_failed_provision_refunds() -> None:
    print("\n[5] Marzban refusing the account refunds the customer in full")
    fake = FakeMarzban(fail_with=ValueError("Marzban create_user failed (409): duplicate"))
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 100_000)

    r = client.post("/api/shop/bot/purchase", headers=BOT_HEADERS,
                    json={"telegram_id": 555, "data_limit_gb": 10})
    check("reported as a failure (502)", r.status_code, 502)
    with Session(engine) as session:
        check("balance fully restored", wallet_balance(session, uid), 100_000)
        order = session.exec(select(ShopOrder)).one()
        check("order marked failed", order.status, ShopOrderStatus.failed)
        refunds = session.exec(
            select(ShopWalletEntry).where(ShopWalletEntry.type == ShopWalletEntryType.refund)
        ).all()
    check("exactly one refund entry", len(refunds), 1)


def test_refund_is_idempotent() -> None:
    print("\n[6] a second refund of the same order credits nothing extra")
    fake = FakeMarzban(fail_with=ValueError("nope"))
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 100_000)
    client.post("/api/shop/bot/purchase", headers=BOT_HEADERS,
                json={"telegram_id": 555, "data_limit_gb": 10})

    with Session(engine) as session:
        order = session.exec(select(ShopOrder)).one()
        before = wallet_balance(session, uid)
        # The sweeper and the provisioning path can both reach an order after
        # a restart. Refunding twice would invent money with nothing to notice
        # it by, since a balance IS the sum of its entries.
        from app.shop_service import refund_order
        refund_order(session, order, reason="second attempt")
        after = wallet_balance(session, uid)
    check("balance unchanged by the second refund", after, before)


def test_stuck_order_sweep() -> None:
    print("\n[7] an order stranded mid-provision is refunded by the sweeper")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 100_000)

    # Simulates the process dying between the debit and Marzban's reply: the
    # order and its debit exist, nothing else does.
    from datetime import timedelta
    with Session(engine) as session:
        order = ShopOrder(shop_user_id=uid, data_limit_gb=10, duration_days=30, price=30_000,
                          status=ShopOrderStatus.provisioning,
                          created_at=utcnow() - timedelta(minutes=STUCK_ORDER_TIMEOUT_MINUTES + 1))
        session.add(order)
        session.flush()
        post_wallet_entry(session, uid, entry_type=ShopWalletEntryType.purchase,
                          amount=-30_000, order_id=order.id, commit=False)
        session.commit()
        check("balance reflects the debit", wallet_balance(session, uid), 70_000)

        swept = sweep_stuck_orders(session)
        check("one order swept", len(swept), 1)
        check("money returned", wallet_balance(session, uid), 100_000)

    # A fresh order must NOT be swept — that would refund a purchase still
    # legitimately in flight.
    with Session(engine) as session:
        fresh = ShopOrder(shop_user_id=uid, data_limit_gb=10, duration_days=30, price=30_000,
                          status=ShopOrderStatus.provisioning)
        session.add(fresh)
        session.commit()
        check("a just-created order is left alone", len(sweep_stuck_orders(session)), 0)


def test_topup_approval_credits_once() -> None:
    print("\n[8] approving the same receipt twice credits it once")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)

    r = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                    json={"telegram_id": 555, "claimed_amount": 50_000, "receipt_file_id": "f123"})
    check("topup created", r.status_code, 200)
    topup_id = r.json()["id"]

    first = client.post(f"/api/shop/topups/{topup_id}/approve", json={})
    second = client.post(f"/api/shop/topups/{topup_id}/approve", json={})
    check("first approval ok", first.status_code, 200)
    check("second approval refused (400)", second.status_code, 400)
    with Session(engine) as session:
        check("credited exactly once", wallet_balance(session, uid), 50_000)


def test_topup_amount_override_and_reject() -> None:
    print("\n[9] the operator can credit a different amount, or reject outright")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)

    a = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                    json={"telegram_id": 555, "claimed_amount": 500_000}).json()["id"]
    client.post(f"/api/shop/topups/{a}/approve", json={"amount": 50_000})
    with Session(engine) as session:
        check("credited the operator's figure, not the claim", wallet_balance(session, uid), 50_000)
        topup = session.get(ShopTopup, a)
        check("the original claim is still on record", topup.claimed_amount, 500_000)

    b = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                    json={"telegram_id": 555, "claimed_amount": 20_000}).json()["id"]
    client.post(f"/api/shop/topups/{b}/reject", json={"reason": "receipt unreadable"})
    with Session(engine) as session:
        check("a rejection credits nothing", wallet_balance(session, uid), 50_000)


def test_closed_shop_and_bounds() -> None:
    print("\n[10] a closed shop sells nothing; volume bounds are enforced")
    fake = FakeMarzban()
    client = _reset(fake, open_shop=False)
    uid = _make_user(client)
    _credit(uid, 1_000_000)

    closed = client.post("/api/shop/bot/purchase", headers=BOT_HEADERS,
                         json={"telegram_id": 555, "data_limit_gb": 10})
    check("closed shop refuses (400)", closed.status_code, 400)
    check("nothing created", fake.created, [])

    client_open = _reset(fake)
    uid2 = _make_user(client_open)
    _credit(uid2, 100_000_000)
    too_small = client_open.post("/api/shop/bot/purchase", headers=BOT_HEADERS,
                                 json={"telegram_id": 555, "data_limit_gb": 1})
    too_big = client_open.post("/api/shop/bot/purchase", headers=BOT_HEADERS,
                               json={"telegram_id": 555, "data_limit_gb": 500})
    check("below min refused", too_small.status_code, 400)
    check("above max refused", too_big.status_code, 400)


def test_cannot_open_shop_half_configured() -> None:
    print("\n[11] the shop refuses to open without a price and a card number")
    fake = FakeMarzban()
    client = _reset(fake, open_shop=False)
    no_price = client.patch("/api/shop/settings", json={"is_open": True, "price_per_gb": 0})
    no_card = client.patch("/api/shop/settings", json={"is_open": True, "card_number": "  "})
    ok = client.patch("/api/shop/settings", json={"is_open": True})
    check("no price -> 400", no_price.status_code, 400)
    check("blank card -> 400", no_card.status_code, 400)
    check("price + card already set -> opens", ok.status_code, 200)
    check("really open", ok.json()["is_open"], True)


def test_accounts_are_scoped_to_their_buyer() -> None:
    print("\n[12] one customer cannot see another's accounts")
    fake = FakeMarzban()
    client = _reset(fake)
    _make_user(client, telegram_id=555)
    buyer = _make_user(client, telegram_id=777)
    _credit(buyer, 100_000)
    client.post("/api/shop/bot/purchase", headers=BOT_HEADERS,
                json={"telegram_id": 777, "data_limit_gb": 10})

    theirs = client.get("/api/shop/bot/accounts", headers=BOT_HEADERS, params={"telegram_id": 777}).json()
    others = client.get("/api/shop/bot/accounts", headers=BOT_HEADERS, params={"telegram_id": 555}).json()
    stranger = client.get("/api/shop/bot/accounts", headers=BOT_HEADERS, params={"telegram_id": 999}).json()
    check("the buyer sees their account", len(theirs), 1)
    check("the other customer sees none", len(others), 0)
    check("an unknown id sees none", stranger, [])


def main() -> int:
    init_db()
    for test in (
        test_auth_boundary,
        test_purchase_happy_path,
        test_insufficient_balance,
        test_no_double_spend,
        test_failed_provision_refunds,
        test_refund_is_idempotent,
        test_stuck_order_sweep,
        test_topup_approval_credits_once,
        test_topup_amount_override_and_reject,
        test_closed_shop_and_bounds,
        test_cannot_open_shop_half_configured,
        test_accounts_are_scoped_to_their_buyer,
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
