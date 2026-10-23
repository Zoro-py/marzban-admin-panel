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
from app.marzban_client import MarzbanUnavailable  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    AccountEvent,
    BillingMode,
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
    """Models a panel that can be QUERIED, not just written to.

    `panel` is what really exists on the far side. `create_lands` decides
    whether a failing create still creates the user — that is the real-world
    case the code has to survive: a read timeout loses the RESPONSE, not
    necessarily the work.
    """

    def __init__(self, fail_with: Exception | None = None, create_lands: bool = False):
        self.fail_with = fail_with
        self.create_lands = create_lands
        self.created: list[str] = []
        self.panel: dict[str, dict] = {}
        # Renewal in place: modify_user can fail, and like create it can fail
        # AFTER the panel already applied it.
        self.modify_fail_with: Exception | None = None
        self.modify_lands = False
        self.modified: list[str] = []

    def _record(self, payload: dict) -> dict:
        username = payload["username"]
        user = {
            "username": username,
            "used_traffic": 0,
            "lifetime_used_traffic": 0,
            "data_limit": payload.get("data_limit"),
            "expire": payload.get("expire"),
            "status": "active",
            "note": payload.get("note"),
            "subscription_url": f"/sub/tok_{username}",
        }
        self.panel[username] = user
        self.created.append(username)
        return user

    async def create_user(self, payload: dict) -> dict:
        if self.fail_with is not None:
            if self.create_lands:
                self._record(payload)
            raise self.fail_with
        return self._record(payload)

    async def get_user(self, username: str):
        return self.panel.get(username)

    async def modify_user(self, username: str, payload: dict) -> dict:
        def apply():
            user = self.panel[username]
            user.update({k: v for k, v in payload.items() if k in ("data_limit", "expire", "status")})
            self.modified.append(username)
            return dict(user)
        if self.modify_fail_with is not None:
            if self.modify_lands:
                apply()
            raise self.modify_fail_with
        return apply()


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
        marzban_module.marzban_client.get_user = fake.get_user
        marzban_module.marzban_client.modify_user = fake.modify_user
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

    # Every bot endpoint, not a representative one. AGENTS.md §4.1: confirm the
    # enforcement runs on the triggering path, not that it exists somewhere in
    # the file. A per-route guard that was missed on ONE endpoint would look
    # identical to its neighbours and pass a single-endpoint test.
    unguarded = []
    for method, path, kwargs in [
        ("post", "/api/shop/bot/session", {"json": {"telegram_id": 1}}),
        ("post", "/api/shop/bot/quote", {"json": {"telegram_id": 1, "data_limit_gb": 10}}),
        ("post", "/api/shop/bot/purchase", {"json": {"telegram_id": 1, "data_limit_gb": 10}}),
        ("post", "/api/shop/bot/purchase/1/deliver", {"json": {"telegram_id": 1}}),
        ("post", "/api/shop/bot/topups", {"json": {"telegram_id": 1, "claimed_amount": 50000}}),
        ("get", "/api/shop/bot/accounts", {"params": {"telegram_id": 1}}),
        ("get", "/api/shop/bot/wallet", {"params": {"telegram_id": 1}}),
        ("post", "/api/shop/bot/orders", {"json": {"telegram_id": 1, "data_limit_gb": 10}}),
        ("post", "/api/shop/bot/orders/1/pay", {"json": {"telegram_id": 1}}),
        ("post", "/api/shop/bot/accounts/1/auto-renew", {"json": {"telegram_id": 1}}),
        ("post", "/api/shop/bot/payg/start", {"json": {"telegram_id": 1}}),
        ("post", "/api/shop/bot/trial", {"json": {"telegram_id": 1}}),
        ("get", "/api/shop/bot/orders/pending", {"params": {"telegram_id": 1}}),
        ("get", "/api/shop/bot/topups/status", {"params": {"telegram_id": 1, "code": "ABCD"}}),
    ]:
        if getattr(client, method)(path, **kwargs).status_code != 401:
            unguarded.append(path)
    check("every bot endpoint refuses an unkeyed request", unguarded, [])

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

        swept = asyncio.run(sweep_stuck_orders(session))
        check("one order swept", len(swept), 1)
        check("money returned", wallet_balance(session, uid), 100_000)

    # A fresh order must NOT be swept — that would refund a purchase still
    # legitimately in flight.
    with Session(engine) as session:
        fresh = ShopOrder(shop_user_id=uid, data_limit_gb=10, duration_days=30, price=30_000,
                          status=ShopOrderStatus.provisioning)
        session.add(fresh)
        session.commit()
        check("a just-created order is left alone", len(asyncio.run(sweep_stuck_orders(session))), 0)


def test_timeout_that_actually_created_is_not_refunded() -> None:
    print("\n[7b] a lost response on a create that SUCCEEDED delivers, it does not refund")
    # The real failure: httpx raises MarzbanUnavailable on a read timeout, but
    # the panel processed the POST. Refunding on that signal alone produced a
    # full refund AND a live account nobody paid for.
    fake = FakeMarzban(fail_with=MarzbanUnavailable("ReadTimeout"), create_lands=True)
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 100_000)

    client.post("/api/shop/bot/purchase", headers=BOT_HEADERS,
                json={"telegram_id": 555, "data_limit_gb": 10})
    with Session(engine) as session:
        order = session.exec(select(ShopOrder)).one()
        balance = wallet_balance(session, uid)
        refunds = session.exec(
            select(ShopWalletEntry).where(ShopWalletEntry.type == ShopWalletEntryType.refund)
        ).all()
    check("the account on the panel is delivered", order.status, ShopOrderStatus.delivered)
    check("the customer was charged for it", balance, 70_000)
    check("no spurious refund", len(refunds), 0)
    check("not a free account", order.marzban_username, "shop1")


def test_sweeper_checks_the_panel_before_refunding() -> None:
    print("\n[7c] a stuck order whose account DOES exist is delivered late, not refunded")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 100_000)

    from datetime import timedelta
    with Session(engine) as session:
        # The process died after Marzban created the user but before the
        # response was handled: order + debit exist, the account exists on the
        # panel, nothing links them.
        order = ShopOrder(shop_user_id=uid, data_limit_gb=10, duration_days=30, price=30_000,
                          status=ShopOrderStatus.provisioning,
                          created_at=utcnow() - timedelta(minutes=STUCK_ORDER_TIMEOUT_MINUTES + 1))
        session.add(order)
        session.flush()
        post_wallet_entry(session, uid, entry_type=ShopWalletEntryType.purchase,
                          amount=-30_000, order_id=order.id, commit=False)
        session.commit()
        oid = order.id
    fake.panel["shop" + str(oid)] = {
        "username": f"shop{oid}", "used_traffic": 0, "lifetime_used_traffic": 0,
        "data_limit": 10 * 1024 ** 3, "expire": None, "status": "active",
        "note": f"shop order #{oid}", "subscription_url": f"/sub/tok_shop{oid}",
    }

    with Session(engine) as session:
        asyncio.run(sweep_stuck_orders(session))
        order = session.get(ShopOrder, oid)
        check("delivered late, not refunded", order.status, ShopOrderStatus.delivered)
        check("customer stays charged for what they have", wallet_balance(session, uid), 70_000)

    # And the opposite: an account the panel has never heard of IS refunded.
    fake.panel.clear()
    with Session(engine) as session:
        order2 = ShopOrder(shop_user_id=uid, data_limit_gb=10, duration_days=30, price=30_000,
                           status=ShopOrderStatus.provisioning,
                           created_at=utcnow() - timedelta(minutes=STUCK_ORDER_TIMEOUT_MINUTES + 1))
        session.add(order2)
        session.flush()
        post_wallet_entry(session, uid, entry_type=ShopWalletEntryType.purchase,
                          amount=-30_000, order_id=order2.id, commit=False)
        session.commit()
        asyncio.run(sweep_stuck_orders(session))
        session.refresh(order2)
    check("no account on the panel -> refunded", order2.status, ShopOrderStatus.failed)


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
        test_timeout_that_actually_created_is_not_refunded,
        test_sweeper_checks_the_panel_before_refunding,
        test_topup_approval_credits_once,
        test_topup_amount_override_and_reject,
        test_closed_shop_and_bounds,
        test_cannot_open_shop_half_configured,
        test_accounts_are_scoped_to_their_buyer,
        test_order_first_approval_delivers,
        test_order_first_short_approval_waits,
        test_order_paid_from_wallet,
        test_order_cannot_be_paid_twice,
        test_pay_is_scoped_to_the_orders_owner,
        test_approval_says_so_when_the_service_could_not_be_built,
        test_topup_cannot_target_someone_elses_order,
        test_trial,
        test_quote_refuses_unbuyable_plans,
        test_renewal_warnings,
        test_accounts_list_has_no_duplicate_after_topup,
        test_force_new_service_does_not_extend,
        test_auto_renew_sufficient_balance,
        test_auto_renew_insufficient_balance_goes_into_debt,
        test_debt_sweep_reminds_suspends_and_reactivates,
        test_payg_not_offered_until_enabled,
        test_payg_start_creates_unlimited_metered_account,
        test_payg_daily_meter_charges_suspends_and_reactivates,
        test_renewal_extends_in_place,
        test_trial_is_upgraded_in_place,
        test_extension_that_landed_is_not_refunded,
        test_extension_that_did_not_land_is_refunded,
        test_existing_customer_gets_no_trial,
        test_overdue_payment_is_announced_once,
        test_receipt_recovery_and_status_by_code,
        test_second_photo_does_not_duplicate_payment,
        test_bridge_service_guards,
        test_approval_cannot_exceed_the_shops_own_ceiling,
        test_sweep_late_delivery_pushes_once,
        test_sweep_refund_pushes_the_refund_notice,
        test_sweep_refund_failure_tells_the_operator,
        test_payg_delivery_uses_payg_caption_not_trial,
        test_purchase_refused_on_payg_effective_account,
        test_falsy_data_limit_is_unlimited,
        test_cap_hit_skips_retail_payg_account,
        test_settings_patch_rate_zero_guard,
        test_suspended_payg_blocks_second_start,
        test_meter_debit_and_baseline_atomic,
        test_reactivation_fires_from_topup_approve,
        test_order_bound_topup_skips_min_topup,
        test_overdue_apology_reaches_typed_receipts,
    ):
        test()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {_failures}")
        return 1
    print("all checks passed")
    return 0


# ── the order-first flow ─────────────────────────────────────────────────
#
# The flow these cover replaced "fund a wallet, wait, come back, buy". What
# they protect is that ONE approval both banks the money and hands over the
# plan — and that it never does the second half for less than the plan costs.


def test_order_first_approval_delivers() -> None:
    print("\n[13] choosing first, then paying: one approval delivers the plan")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)

    intent = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                         json={"telegram_id": 555, "data_limit_gb": 10}).json()
    check("price is the plan's", intent["price"], 30_000)
    check("the whole price is still to pay", intent["shortfall"], 30_000)
    check("not payable from an empty wallet", intent["payable_from_wallet"], False)
    check("choosing created no account", fake.created, [])

    topup = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                        json={"telegram_id": 555, "claimed_amount": 30_000,
                              "receipt_file_id": "r1", "order_id": intent["order_id"]}).json()
    check("the customer gets a reference code", bool(topup.get("reference_code")), True)
    check("the payment remembers its order", topup.get("order_id"), intent["order_id"])

    # The receipt itself now buys a bridge service, so one account exists
    # BEFORE the operator has decided anything.
    check("a bridge service was handed over on the receipt", len(fake.created), 1)

    r = client.post(f"/api/shop/topups/{topup['id']}/approve", json={})
    check("approval ok", r.status_code, 200)
    with Session(engine) as session:
        order = session.get(ShopOrder, intent["order_id"])
        check("the plan was delivered by the approval itself", order.status, ShopOrderStatus.delivered)
        check("paid exactly once — nothing left over", wallet_balance(session, uid), 0)
        check("and it extended the bridge rather than making a second account",
              order.extends_account_id is not None, True)
    check("still exactly one account on the panel", len(fake.created), 1)


def test_order_first_short_approval_waits() -> None:
    print("\n[14] an approval for LESS than the plan banks the money and delivers nothing")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)

    intent = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                         json={"telegram_id": 555, "data_limit_gb": 10}).json()
    topup = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                        json={"telegram_id": 555, "claimed_amount": 30_000,
                              "order_id": intent["order_id"]}).json()
    client.post(f"/api/shop/topups/{topup['id']}/approve", json={"amount": 10_000})
    with Session(engine) as session:
        order = session.get(ShopOrder, intent["order_id"])
        check("the plan still waits for payment", order.status, ShopOrderStatus.awaiting_payment)
        check("the money that did arrive is kept", wallet_balance(session, uid), 10_000)
    # The customer CLAIMED the full price, so the bridge was granted on the
    # receipt; what a partial approval must not do is deliver the plan.
    check("the plan itself created no account", len(fake.created), 1)
    with Session(engine) as session:
        plan = session.get(ShopOrder, intent["order_id"])
        check("the paid plan has no account yet", plan.account_id, None)

    # And the next quote knows about that credit: only the rest is asked for.
    again = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                        json={"telegram_id": 555, "data_limit_gb": 10}).json()
    check("the shortfall counts the credit already held", again["shortfall"], 20_000)


def test_order_paid_from_wallet() -> None:
    print("\n[15] a funded wallet pays an order in one tap, no card, no human")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 100_000)

    intent = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                         json={"telegram_id": 555, "data_limit_gb": 10}).json()
    check("payable from the wallet", intent["payable_from_wallet"], True)
    check("nothing left to transfer — never a negative number", intent["shortfall"], 0)

    r = client.post(f"/api/shop/bot/orders/{intent['order_id']}/pay", headers=BOT_HEADERS, json={"telegram_id": 555})
    check("http 200", r.status_code, 200)
    with Session(engine) as session:
        check("debited once", wallet_balance(session, uid), 70_000)
    check("delivered", len(fake.created), 1)


def test_order_cannot_be_paid_twice() -> None:
    print("\n[16] paying the same order twice charges once")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 100_000)

    oid = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                      json={"telegram_id": 555, "data_limit_gb": 10}).json()["order_id"]
    first = client.post(f"/api/shop/bot/orders/{oid}/pay", headers=BOT_HEADERS, json={"telegram_id": 555})
    second = client.post(f"/api/shop/bot/orders/{oid}/pay", headers=BOT_HEADERS, json={"telegram_id": 555})
    check("first ok", first.status_code, 200)
    check("second refused (400)", second.status_code, 400)
    with Session(engine) as session:
        check("charged once", wallet_balance(session, uid), 70_000)
    check("one account", len(fake.created), 1)


def test_topup_cannot_target_someone_elses_order() -> None:
    print("\n[17] a payment cannot be pointed at another customer's order")
    fake = FakeMarzban()
    client = _reset(fake)
    _make_user(client, telegram_id=555)
    _make_user(client, telegram_id=777)

    victim_order = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                               json={"telegram_id": 555, "data_limit_gb": 10}).json()["order_id"]
    r = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                    json={"telegram_id": 777, "claimed_amount": 30_000, "order_id": victim_order})
    check("refused (400)", r.status_code, 400)


def test_trial() -> None:
    print("\n[18] the free trial: real, once per person, off unless switched on, hours honoured")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)

    off = client.post("/api/shop/bot/trial", headers=BOT_HEADERS, json={"telegram_id": 555})
    check("refused while trials are switched off", off.status_code, 400)
    check("nothing created while off", fake.created, [])

    client.patch("/api/shop/settings", json={"trial_enabled": True, "trial_gb": 1, "trial_hours": 6})
    session_before = client.post("/api/shop/bot/session", headers=BOT_HEADERS,
                                 json={"telegram_id": 555}).json()
    check("offered to a newcomer", session_before["trial_available"], True)

    import time as _time
    started = _time.time()
    first = client.post("/api/shop/bot/trial", headers=BOT_HEADERS, json={"telegram_id": 555})
    check("granted", first.status_code, 200)
    check("costs nothing", first.json()["price"], 0)
    with Session(engine) as session:
        check("the wallet is untouched", wallet_balance(session, uid), 0)
    username = fake.created[0]
    lasts = fake.panel[username]["expire"] - started
    # 6 hours, not rounded up to a day: ShopOrder stores whole days, and an
    # earlier draft let that turn a 6-hour trial into a 24-hour one.
    check("expires after the configured hours, not a whole day",
          6 * 3600 - 60 <= lasts <= 6 * 3600 + 60, True)

    second = client.post("/api/shop/bot/trial", headers=BOT_HEADERS, json={"telegram_id": 555})
    check("a second trial is refused", second.status_code, 400)
    check("still exactly one account", len(fake.created), 1)
    session_after = client.post("/api/shop/bot/session", headers=BOT_HEADERS,
                                json={"telegram_id": 555}).json()
    check("no longer offered", session_after["trial_available"], False)
    check("but the shop still says it has trials", session_after["trial_enabled"], True)


def test_quote_refuses_unbuyable_plans() -> None:
    print("\n[19] a quote refuses exactly what a purchase would refuse")
    fake = FakeMarzban()
    client = _reset(fake)
    _make_user(client)
    huge = client.post("/api/shop/bot/quote", headers=BOT_HEADERS,
                       json={"telegram_id": 555, "data_limit_gb": 5000})
    check("over the maximum -> 400", huge.status_code, 400)

    client = _reset(fake, open_shop=False)
    _make_user(client)
    closed = client.post("/api/shop/bot/quote", headers=BOT_HEADERS,
                         json={"telegram_id": 555, "data_limit_gb": 10})
    check("shop closed -> 400", closed.status_code, 400)


def test_renewal_warnings() -> None:
    print("")
    print("[20] renewal warnings: before the service ends, once each, retried if unsent")
    from app import notify as notify_module
    from app.shop_service import warn_customers_before_service_ends

    sent: list[tuple[int, str]] = []

    async def capture(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    async def broken(chat_id, text, reply_markup=None):
        raise RuntimeError("telegram down")

    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 100_000)
    oid = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                      json={"telegram_id": 555, "data_limit_gb": 10}).json()["order_id"]
    client.post(f"/api/shop/bot/orders/{oid}/pay", headers=BOT_HEADERS, json={"telegram_id": 555})

    import time as _time
    original = notify_module.send_to_shop_user
    try:
        with Session(engine) as session:
            order = session.get(ShopOrder, oid)
            account = session.get(Account, order.account_id)
            account.expire = int(_time.time()) + 20 * 86400
            account.used_traffic = int(account.data_limit * 0.10)
            # The 24h post-delivery grace (2026-10-01: a paid customer heard
            # "94% used" one minute after paying) would swallow every warning
            # below — these scenarios are "the service has been RUNNING for a
            # while", so backdate the delivery past the grace window.
            from datetime import timedelta as _td
            order.delivered_at = utcnow() - _td(days=8)
            session.add(account)
            session.add(order)
            session.commit()

            notify_module.send_to_shop_user = capture
            check("nothing to say while far from the end",
                  asyncio.run(warn_customers_before_service_ends(session)), 0)

            account.expire = int(_time.time()) + 2 * 86400
            session.add(account)
            session.commit()

            # A failed send must NOT mark the order, or the customer never hears.
            notify_module.send_to_shop_user = broken
            check("a failed send counts as not sent",
                  asyncio.run(warn_customers_before_service_ends(session)), 0)
            session.refresh(order)
            check("...and leaves the order unmarked", order.expiry_warned_at, None)

            notify_module.send_to_shop_user = capture
            check("expiry warning sent once Telegram is back",
                  asyncio.run(warn_customers_before_service_ends(session)), 1)
            check("never twice", asyncio.run(warn_customers_before_service_ends(session)), 0)

            account.used_traffic = int(account.data_limit * 0.85)
            session.add(account)
            session.commit()
            check("usage warning is separate and also once",
                  asyncio.run(warn_customers_before_service_ends(session)), 1)
            check("usage never twice", asyncio.run(warn_customers_before_service_ends(session)), 0)

            account.status = "disabled"
            order.expiry_warned_at = None
            order.usage_warned_at = None
            session.add(account)
            session.add(order)
            session.commit()
            check("a disabled account is not the customer's to renew",
                  asyncio.run(warn_customers_before_service_ends(session)), 0)
    finally:
        notify_module.send_to_shop_user = original

    # A trial is warned in hours, not days.
    fake = FakeMarzban()
    client = _reset(fake)
    _make_user(client, telegram_id=888)
    client.patch("/api/shop/settings", json={"trial_enabled": True, "trial_gb": 1, "trial_hours": 6})
    client.post("/api/shop/bot/trial", headers=BOT_HEADERS, json={"telegram_id": 888})
    sent.clear()
    try:
        notify_module.send_to_shop_user = capture
        with Session(engine) as session:
            trial = session.exec(select(ShopOrder).where(ShopOrder.price == 0)).one()
            check("six hours left: too early to nudge",
                  asyncio.run(warn_customers_before_service_ends(session)), 0)
            account = session.get(Account, trial.account_id)
            account.expire = int(_time.time()) + 3600
            session.add(account)
            session.commit()
            check("one hour left: nudged", asyncio.run(warn_customers_before_service_ends(session)), 1)
            check("the nudge is the trial one", "تست" in sent[-1][1], True)
    finally:
        notify_module.send_to_shop_user = original


GB_BYTES = 1024 ** 3


def _buy_from_wallet(client, gb: float) -> int:
    oid = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                      json={"telegram_id": 555, "data_limit_gb": gb}).json()["order_id"]
    client.post(f"/api/shop/bot/orders/{oid}/pay", headers=BOT_HEADERS, json={"telegram_id": 555})
    return oid


def test_accounts_list_has_no_duplicate_after_topup() -> None:
    print("\n[21b] two delivered orders on the same account -> ONE row in the bot's account list, not two")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 200_000)

    _buy_from_wallet(client, 10)
    _buy_from_wallet(client, 20)  # extends the same account - this is the top-up from the live screenshot

    rows = client.get("/api/shop/bot/accounts", headers=BOT_HEADERS, params={"telegram_id": 555}).json()
    check("one row for the one account, not one per order", len(rows), 1)
    check("it shows the live stacked total (10 + 20 GB), not either order alone",
          rows[0]["data_limit"], 30 * GB_BYTES)


def test_force_new_service_does_not_extend() -> None:
    print("\n[21c] MENU_NEW_SERVICE (force_new_service) always mints a second, independent account")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 200_000)

    first_oid = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                            json={"telegram_id": 555, "data_limit_gb": 10}).json()["order_id"]
    client.post(f"/api/shop/bot/orders/{first_oid}/pay", headers=BOT_HEADERS, json={"telegram_id": 555})
    first_username = fake.created[0]

    second_oid = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                             json={"telegram_id": 555, "data_limit_gb": 20, "force_new_service": True}
                             ).json()["order_id"]
    client.post(f"/api/shop/bot/orders/{second_oid}/pay", headers=BOT_HEADERS, json={"telegram_id": 555})

    check("a SECOND account was created, not an extension", len(fake.created), 2)
    check("the first account was never modified", fake.modified, [])
    with Session(engine) as session:
        o1 = session.get(ShopOrder, first_oid)
        o2 = session.get(ShopOrder, second_oid)
        check("second order did not extend the first account", o2.extends_account_id, None)
        check("second order's own account differs from the first", o2.account_id != o1.account_id, True)
        check("two local account rows, not one", len(session.exec(select(Account)).all()), 2)

    rows = client.get("/api/shop/bot/accounts", headers=BOT_HEADERS, params={"telegram_id": 555}).json()
    check("the bot's list shows both as separate services", len(rows), 2)


def _setup_account_for_auto_renew(client, fake, *, gbs=(10, 20)) -> tuple[int, str]:
    """A shop user with `len(gbs)` real paid orders on ONE account — the
    average-volume history the auto-renew button reads. Credited EXACTLY
    enough to pay for those orders (price_per_gb=3000, see _reset), so the
    wallet lands at precisely 0 afterward — deterministic ground for both
    the "enough balance" test (which tops up more on top of this) and the
    "goes into debt" test (which doesn't). Returns (account_id, username)."""
    uid = _make_user(client)
    _credit(uid, sum(int(-(-gb * 3000 // 1)) for gb in gbs))
    for gb in gbs:
        oid = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                          json={"telegram_id": 555, "data_limit_gb": gb}).json()["order_id"]
        client.post(f"/api/shop/bot/orders/{oid}/pay", headers=BOT_HEADERS, json={"telegram_id": 555})
    with Session(engine) as session:
        account = session.exec(select(Account)).first()
        return account.id, account.marzban_username


def test_auto_renew_sufficient_balance() -> None:
    print("\n[33] auto-renew with enough balance: average volume, no debt framing")
    fake = FakeMarzban()
    client = _reset(fake)
    account_id, username = _setup_account_for_auto_renew(client, fake, gbs=(10, 20))
    # Average of 10 and 20 is 15 GB at 3000/GB = 45,000 T — leave plenty.
    with Session(engine) as session:
        shop_user_id = session.exec(select(ShopUser)).first().id
    _credit(shop_user_id, 500_000)

    r = client.post(f"/api/shop/bot/accounts/{account_id}/auto-renew", headers=BOT_HEADERS,
                    json={"telegram_id": 555})
    check("200 OK", r.status_code, 200)
    body = r.json()
    check("average of 10 and 20 GB is 15", body["data_limit_gb"], 15.0)
    check("stayed non-negative", body["went_negative"], False)
    check("owed is 0", body["owed"], 0)
    with Session(engine) as session:
        account = session.get(Account, account_id)
        check("no debt recorded", account.debt_since, None)
    # Setup's own 2nd purchase already extended this account once; the
    # auto-renew call must be a SECOND extend of the SAME account, not a
    # new one.
    check("every modify call was on the same account, no new one created", set(fake.modified), {username})
    check("still only one account ever created", fake.created, [username])


def test_auto_renew_insufficient_balance_goes_into_debt() -> None:
    print("\n[34] auto-renew with insufficient balance: still renews, wallet goes negative, bounded")
    fake = FakeMarzban()
    client = _reset(fake)
    account_id, username = _setup_account_for_auto_renew(client, fake, gbs=(10, 20))
    # The two setup purchases already spent the wallet down near zero —
    # average renewal price (15 GB * 3000 = 45,000) should push it negative.

    r = client.post(f"/api/shop/bot/accounts/{account_id}/auto-renew", headers=BOT_HEADERS,
                    json={"telegram_id": 555})
    check("200 OK even though balance went negative", r.status_code, 200)
    body = r.json()
    check("renewed anyway", body["went_negative"], True)
    check("owed matches the negative balance", body["owed"], -body["balance"])
    check("the account really was extended (modified), not refused", username in fake.modified, True)
    with Session(engine) as session:
        account = session.get(Account, account_id)
        check("debt_since recorded", account.debt_since is not None, True)

    # A second auto-renew is refused while the first debt is unpaid.
    r2 = client.post(f"/api/shop/bot/accounts/{account_id}/auto-renew", headers=BOT_HEADERS,
                     json={"telegram_id": 555})
    check("refused: already in debt", r2.status_code, 409)
    check("no second auto-renew order got created", "ALREADY_IN_DEBT" in r2.json()["detail"], True)


def test_debt_sweep_reminds_suspends_and_reactivates() -> None:
    print("\n[35] debt sweep: reminder at day 3, suspend at day 5, reactivate once paid off")
    import asyncio
    from datetime import timedelta
    from app.shop_service import _sweep_debt_accounts

    fake = FakeMarzban()
    client = _reset(fake)
    account_id, username = _setup_account_for_auto_renew(client, fake, gbs=(10, 20))
    client.post(f"/api/shop/bot/accounts/{account_id}/auto-renew", headers=BOT_HEADERS,
               json={"telegram_id": 555})

    with Session(engine) as session:
        account = session.get(Account, account_id)
        assert account.debt_since is not None
        account.debt_since = utcnow() - timedelta(days=3, minutes=1)
        session.add(account)
        session.commit()

    with Session(engine) as session:
        result = asyncio.run(_sweep_debt_accounts(session))
    check("one reminder sent at day 3", result["reminded"], 1)
    check("not suspended yet at day 3", result["suspended"], 0)
    with Session(engine) as session:
        account = session.get(Account, account_id)
        check("reminder timestamp recorded", account.debt_reminder_sent_at is not None, True)

    with Session(engine) as session:
        result = asyncio.run(_sweep_debt_accounts(session))
    check("no second reminder on the same episode", result["reminded"], 0)

    with Session(engine) as session:
        account = session.get(Account, account_id)
        account.debt_since = utcnow() - timedelta(days=5, minutes=1)
        session.add(account)
        session.commit()
    with Session(engine) as session:
        result = asyncio.run(_sweep_debt_accounts(session))
    check("suspended at day 5", result["suspended"], 1)
    check("the account was actually disabled in Marzban", fake.panel[username]["status"], "disabled")
    with Session(engine) as session:
        account = session.get(Account, account_id)
        check("auto_suspended flag set", account.auto_suspended, True)

    # Top up enough to clear the debt — should auto-reactivate.
    with Session(engine) as session:
        shop_user_id = session.exec(select(ShopUser)).first().id
    _credit(shop_user_id, 500_000)

    with Session(engine) as session:
        result = asyncio.run(_sweep_debt_accounts(session))
    check("reactivated once paid off", result["reactivated"], 1)
    check("Marzban account is active again", fake.panel[username]["status"], "active")
    with Session(engine) as session:
        account = session.get(Account, account_id)
        check("debt_since cleared", account.debt_since, None)
        check("auto_suspended cleared", account.auto_suspended, False)


def _enable_payg(rate_per_gb: int) -> None:
    with Session(engine) as session:
        settings = session.get(ShopSettings, 1)
        settings.payg_enabled = True
        settings.payg_rate_per_gb = rate_per_gb
        session.add(settings)
        session.commit()


def test_payg_not_offered_until_enabled() -> None:
    print("\n[36] payg refused until the operator sets a rate and turns it on")
    fake = FakeMarzban()
    client = _reset(fake)
    _make_user(client)

    r = client.post("/api/shop/bot/payg/start", headers=BOT_HEADERS, json={"telegram_id": 555})
    check("refused: not available", r.status_code, 409)
    check("stable prefix", "PAYG_NOT_AVAILABLE" in r.json()["detail"], True)
    check("nothing created", fake.created, [])

    # Also the session the bot reads for its menu: button must be hidden.
    session_info = client.post("/api/shop/bot/session", headers=BOT_HEADERS, json={"telegram_id": 555}).json()
    check("not advertised in the menu session either", session_info["payg_enabled"], False)


def test_payg_start_creates_unlimited_metered_account() -> None:
    print("\n[37] starting payg: no money up front, unlimited Marzban cap, billing_mode=payg")
    fake = FakeMarzban()
    client = _reset(fake)
    _make_user(client)
    _enable_payg(500)

    r = client.post("/api/shop/bot/payg/start", headers=BOT_HEADERS, json={"telegram_id": 555})
    check("200 OK", r.status_code, 200)
    body = r.json()
    check("delivered", body["status"], "delivered")
    check("one account created", len(fake.created), 1)
    username = fake.created[0]
    check("Marzban sees it as unlimited (data_limit falsy)", bool(fake.panel[username]["data_limit"]), False)
    with Session(engine) as session:
        account = session.exec(select(Account)).first()
        check("billing_mode is payg", account.billing_mode, BillingMode.payg)
        check("wallet untouched — no money moves on start", wallet_balance(session, _shop_user_id(session)), 0)

    # A second start is refused — this is a second billing relationship,
    # not something you get twice.
    r2 = client.post("/api/shop/bot/payg/start", headers=BOT_HEADERS, json={"telegram_id": 555})
    check("refused: already has a service", r2.status_code, 409)
    check("stable prefix", "ALREADY_HAS_SERVICE" in r2.json()["detail"], True)
    check("still only one account", len(fake.created), 1)


def _shop_user_id(session) -> int:
    return session.exec(select(ShopUser)).first().id


def test_payg_daily_meter_charges_suspends_and_reactivates() -> None:
    print("\n[38] payg daily meter: charges real usage, suspends with no grace, reactivates on top-up")
    import asyncio
    from app.shop_service import _sweep_payg_accounts

    fake = FakeMarzban()
    client = _reset(fake)
    _make_user(client)
    _enable_payg(500)  # 500 T/GB
    client.post("/api/shop/bot/payg/start", headers=BOT_HEADERS, json={"telegram_id": 555})
    username = fake.created[0]
    with Session(engine) as session:
        shop_user_id = _shop_user_id(session)
    _credit(shop_user_id, 1000)  # covers exactly 2 GB

    # Simulate 1 GB of real usage since baseline.
    with Session(engine) as session:
        account = session.exec(select(Account)).first()
        account.used_traffic = GB_BYTES
        session.add(account)
        session.commit()

    with Session(engine) as session:
        result = asyncio.run(_sweep_payg_accounts(session))
    check("one account charged", result["charged"], 1)
    with Session(engine) as session:
        account = session.exec(select(Account)).first()
        check("baseline advanced to used_traffic", account.usage_baseline, GB_BYTES)
        check("not suspended — balance still positive (1000-500=500)", account.auto_suspended, False)
    check("Marzban account untouched (still active)", fake.panel[username]["status"], "active")

    # Another 1 GB of usage — this charge (500) takes the balance to 0, not
    # negative, so it must NOT suspend (only a NEGATIVE balance does).
    with Session(engine) as session:
        account = session.exec(select(Account)).first()
        account.used_traffic = GB_BYTES * 2
        session.add(account)
        session.commit()
    with Session(engine) as session:
        result = asyncio.run(_sweep_payg_accounts(session))
    check("charged again", result["charged"], 1)
    with Session(engine) as session:
        account = session.exec(select(Account)).first()
        check("still not suspended at exactly 0", account.auto_suspended, False)

    # A third GB — now the charge takes it negative: immediate suspend, no
    # grace window (unlike the bounded-debt auto-renew feature).
    with Session(engine) as session:
        account = session.exec(select(Account)).first()
        account.used_traffic = GB_BYTES * 3
        session.add(account)
        session.commit()
    with Session(engine) as session:
        result = asyncio.run(_sweep_payg_accounts(session))
    check("suspended immediately, no grace", result["suspended"], 1)
    check("Marzban account actually disabled", fake.panel[username]["status"], "disabled")
    with Session(engine) as session:
        account = session.exec(select(Account)).first()
        check("auto_suspended flag set", account.auto_suspended, True)

    # Top up enough to clear the negative balance — must reactivate on the
    # very next tick, with NO further usage needed.
    _credit(shop_user_id, 1000)
    with Session(engine) as session:
        result = asyncio.run(_sweep_payg_accounts(session))
    check("reactivated", result["reactivated"], 1)
    check("Marzban account active again", fake.panel[username]["status"], "active")
    with Session(engine) as session:
        account = session.exec(select(Account)).first()
        check("auto_suspended cleared", account.auto_suspended, False)


def test_renewal_extends_in_place() -> None:
    print("\n[21] a second purchase EXTENDS the existing account: same link, stacked volume and days")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 200_000)

    first = _buy_from_wallet(client, 10)
    username = fake.created[0]
    expire_after_first = fake.panel[username]["expire"]
    second = _buy_from_wallet(client, 20)

    check("no second account was created", fake.created, [username])
    check("the existing account was modified once", fake.modified, [username])
    check("volume stacked: 10 + 20 GB", fake.panel[username]["data_limit"], 30 * GB_BYTES)
    added = fake.panel[username]["expire"] - expire_after_first
    check("days stacked onto the remaining time, not reset",
          30 * 86400 - 120 <= added <= 30 * 86400 + 120, True)
    with Session(engine) as session:
        o1 = session.get(ShopOrder, first)
        o2 = session.get(ShopOrder, second)
        check("renewal delivered", o2.status, ShopOrderStatus.delivered)
        check("renewal points at the same account", o2.account_id, o1.account_id)
        check("renewal records what it extended", o2.extends_account_id, o1.account_id)
        check("charged for both plans exactly once", wallet_balance(session, uid), 200_000 - 30_000 - 60_000)
        check("one local account row, not two", len(session.exec(select(Account)).all()), 1)


def test_trial_is_upgraded_in_place() -> None:
    print("\n[22] buying after the trial keeps the trial link working ('so you are not cut off' is true)")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    client.patch("/api/shop/settings", json={"trial_enabled": True, "trial_gb": 1, "trial_hours": 24})
    client.post("/api/shop/bot/trial", headers=BOT_HEADERS, json={"telegram_id": 555})
    trial_user = fake.created[0]
    _credit(uid, 100_000)
    _buy_from_wallet(client, 10)
    check("still the trial's account", fake.created, [trial_user])
    check("trial 1 GB + paid 10 GB", fake.panel[trial_user]["data_limit"], 11 * GB_BYTES)


def test_extension_that_landed_is_not_refunded() -> None:
    print("\n[23] a renewal whose modify timed out but LANDED is delivered, not refunded")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 200_000)
    _buy_from_wallet(client, 10)
    username = fake.created[0]

    fake.modify_fail_with = MarzbanUnavailable("ReadTimeout")
    fake.modify_lands = True
    second = _buy_from_wallet(client, 20)
    with Session(engine) as session:
        check("delivered", session.get(ShopOrder, second).status, ShopOrderStatus.delivered)
        check("charged, not refunded", wallet_balance(session, uid), 200_000 - 30_000 - 60_000)
        refunds = session.exec(select(ShopWalletEntry).where(
            ShopWalletEntry.type == ShopWalletEntryType.refund)).all()
        check("no refund entry", len(refunds), 0)
    check("volume really on the panel", fake.panel[username]["data_limit"], 30 * GB_BYTES)


def test_extension_that_did_not_land_is_refunded() -> None:
    print("\n[24] a renewal that never reached the panel is refunded, and the panel is untouched")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 200_000)
    _buy_from_wallet(client, 10)
    username = fake.created[0]

    fake.modify_fail_with = MarzbanUnavailable("connection refused")
    fake.modify_lands = False
    second = _buy_from_wallet(client, 20)
    with Session(engine) as session:
        check("marked failed", session.get(ShopOrder, second).status, ShopOrderStatus.failed)
        check("money back: only the first plan charged", wallet_balance(session, uid), 200_000 - 30_000)
    check("panel still shows only the first plan", fake.panel[username]["data_limit"], 10 * GB_BYTES)


def test_existing_customer_gets_no_trial() -> None:
    print("\n[25] someone who already has a service is not offered a free trial")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    client.patch("/api/shop/settings", json={"trial_enabled": True})
    _credit(uid, 100_000)
    _buy_from_wallet(client, 10)
    sess = client.post("/api/shop/bot/session", headers=BOT_HEADERS, json={"telegram_id": 555}).json()
    check("not offered", sess["trial_available"], False)
    r = client.post("/api/shop/bot/trial", headers=BOT_HEADERS, json={"telegram_id": 555})
    check("refused if asked anyway", r.status_code, 400)


def test_overdue_payment_is_announced_once() -> None:
    print("\n[26] a payment past its promised time is announced as late, once")
    from datetime import timedelta
    from app import notify as notify_module
    from app.shop_service import notify_overdue_payments

    sent = []

    async def capture(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    async def admin_capture(text, reply_markup=None, *, thread_id=None):
        sent.append(("admin", text))

    fake = FakeMarzban()
    client = _reset(fake)
    _make_user(client)
    tid = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                      json={"telegram_id": 555, "claimed_amount": 50_000, "receipt_file_id": "r"}).json()["id"]
    original, original_admin = notify_module.send_to_shop_user, notify_module.notify_admin
    notify_module.send_to_shop_user = capture
    notify_module.notify_admin = admin_capture
    try:
        with Session(engine) as session:
            check("not late yet", asyncio.run(notify_overdue_payments(session)), 0)
            topup = session.get(ShopTopup, tid)
            topup.created_at = utcnow() - timedelta(minutes=45)
            session.add(topup)
            session.commit()
            check("late: announced", asyncio.run(notify_overdue_payments(session)), 1)
            check("never twice", asyncio.run(notify_overdue_payments(session)), 0)
        check("the customer was told", any(who == 555 for who, _ in sent), True)
        check("the operator was told too", any(who == "admin" for who, _ in sent), True)
    finally:
        notify_module.send_to_shop_user, notify_module.notify_admin = original, original_admin


def test_receipt_recovery_and_status_by_code() -> None:
    print("\n[27] a lost conversation can still find its order; a code returns its payment's status")
    fake = FakeMarzban()
    client = _reset(fake)
    _make_user(client)
    _make_user(client, telegram_id=777)

    none_yet = client.get("/api/shop/bot/orders/pending", headers=BOT_HEADERS, params={"telegram_id": 555})
    check("nothing pending -> null", none_yet.json(), None)

    oid = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                      json={"telegram_id": 555, "data_limit_gb": 10}).json()["order_id"]
    pending = client.get("/api/shop/bot/orders/pending", headers=BOT_HEADERS,
                         params={"telegram_id": 555}).json()
    check("the waiting order is found", pending["order_id"], oid)
    check("with the amount still to pay", pending["shortfall"], 30_000)

    topup = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                        json={"telegram_id": 555, "claimed_amount": 30_000, "order_id": oid}).json()
    code = topup["reference_code"]
    status = client.get("/api/shop/bot/topups/status", headers=BOT_HEADERS,
                        params={"telegram_id": 555, "code": code.lower()}).json()
    check("status by code (case-insensitive)", status["status"], "pending")

    foreign = client.get("/api/shop/bot/topups/status", headers=BOT_HEADERS,
                         params={"telegram_id": 777, "code": code})
    check("another customer cannot read it", foreign.status_code, 404)

    client.post(f"/api/shop/topups/{topup['id']}/approve", json={})
    after = client.get("/api/shop/bot/topups/status", headers=BOT_HEADERS,
                       params={"telegram_id": 555, "code": code}).json()
    check("after approval: delivered", after["order_status"], "delivered")


def test_second_photo_does_not_duplicate_payment() -> None:
    print("")
    print("[28] an order already under review is not recovered for a second receipt")
    fake = FakeMarzban()
    client = _reset(fake)
    _make_user(client)
    oid = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                      json={"telegram_id": 555, "data_limit_gb": 10}).json()["order_id"]
    first = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                        json={"telegram_id": 555, "claimed_amount": 30_000, "order_id": oid}).json()
    again = client.get("/api/shop/bot/orders/pending", headers=BOT_HEADERS, params={"telegram_id": 555})
    check("not offered again while its receipt is pending", again.json(), None)

    client.post(f"/api/shop/topups/{first['id']}/reject", json={"reason": "blurry"})
    after_reject = client.get("/api/shop/bot/orders/pending", headers=BOT_HEADERS,
                              params={"telegram_id": 555}).json()
    check("offered again once the operator rejected it", after_reject["order_id"], oid)


def test_pay_is_scoped_to_the_orders_owner() -> None:
    print("")
    print("[29] one customer cannot pay, or fetch, another customer's order")
    fake = FakeMarzban()
    client = _reset(fake)
    mine = _make_user(client, telegram_id=555)
    _make_user(client, telegram_id=777)
    _credit(mine, 100_000)
    oid = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                      json={"telegram_id": 555, "data_limit_gb": 10}).json()["order_id"]

    stranger = client.post(f"/api/shop/bot/orders/{oid}/pay", headers=BOT_HEADERS,
                           json={"telegram_id": 777})
    check("a stranger gets 404, not someone else's plan", stranger.status_code, 404)
    check("nothing was created", len(fake.created), 0)

    stranger_qr = client.post(f"/api/shop/bot/purchase/{oid}/deliver", headers=BOT_HEADERS,
                              json={"telegram_id": 777})
    check("nor can they have its QR", stranger_qr.status_code, 404)

    owner = client.post(f"/api/shop/bot/orders/{oid}/pay", headers=BOT_HEADERS,
                        json={"telegram_id": 555})
    check("the owner still can", owner.status_code, 200)


def test_approval_says_so_when_the_service_could_not_be_built() -> None:
    print("")
    print("[30] a payment approved for an order that then FAILS says so, and says the money is back")
    fake = FakeMarzban(fail_with=MarzbanUnavailable("panel down"))
    client = _reset(fake)
    _make_user(client)
    oid = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                      json={"telegram_id": 555, "data_limit_gb": 10}).json()["order_id"]
    topup = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                        json={"telegram_id": 555, "claimed_amount": 30_000, "order_id": oid}).json()

    sent: list[tuple[int, str]] = []

    async def capture(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    # The endpoint imported the function by name, so the patch has to land in
    # the router's namespace, not notify's.
    from app.routers import shop as shop_router
    original = shop_router.send_to_shop_user
    shop_router.send_to_shop_user = capture
    try:
        r = client.post(f"/api/shop/topups/{topup['id']}/approve", json={})
    finally:
        shop_router.send_to_shop_user = original

    check("the approval itself succeeds", r.status_code, 200)
    with Session(engine) as session:
        order = session.get(ShopOrder, oid)
        check("the order is marked failed", order.status.value, "failed")
        check("the money is in the wallet, not spent", wallet_balance(session, order.shop_user_id), 30_000)
    body = sent[-1][1] if sent else ""
    check("the customer is told the service was not built", "ساخت سرویس" in body, True)
    check("and is not asked for the rest of the money", "شماره کارت" in body, False)


def test_bridge_service_guards() -> None:
    print("")
    print("[31] the bridge service is guarded: not for a token claim, and never again after a rejection")
    fake = FakeMarzban()
    client = _reset(fake)
    _make_user(client)

    # A receipt claiming far less than the plan costs buys nothing.
    small = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                        json={"telegram_id": 555, "data_limit_gb": 10}).json()
    client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                json={"telegram_id": 555, "claimed_amount": 10_000, "order_id": small["order_id"]})
    check("a token claim earns no bridge", len(fake.created), 0)

    # A full claim does.
    full = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                       json={"telegram_id": 555, "data_limit_gb": 10}).json()
    topup = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                        json={"telegram_id": 555, "claimed_amount": 30_000,
                              "order_id": full["order_id"]}).json()
    check("a full claim does", len(fake.created), 1)
    bridge_name = fake.created[0]

    # Rejecting it stops the bridge...
    client.post(f"/api/shop/topups/{topup['id']}/reject", json={"reason": "no such transfer"})
    check("the bridge account is disabled", fake.panel[bridge_name]["status"], "disabled")

    # ...and there is no second one for the next receipt.
    again = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                        json={"telegram_id": 555, "data_limit_gb": 10}).json()
    client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                json={"telegram_id": 555, "claimed_amount": 30_000, "order_id": again["order_id"]})
    check("no bridge after a rejection", len(fake.created), 1)


def test_approval_cannot_exceed_the_shops_own_ceiling() -> None:
    print("")
    print("[32] an approval above max_topup is refused — one extra zero must not credit ten times the money")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    topup = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                        json={"telegram_id": 555, "claimed_amount": 100_000}).json()

    fat = client.post(f"/api/shop/topups/{topup['id']}/approve", json={"amount": 100_000_000})
    check("refused (400)", fat.status_code, 400)
    with Session(engine) as session:
        check("nothing credited", wallet_balance(session, uid), 0)

    ok = client.post(f"/api/shop/topups/{topup['id']}/approve", json={"amount": 100_000})
    check("the real amount still goes through", ok.status_code, 200)
    with Session(engine) as session:
        check("credited once", wallet_balance(session, uid), 100_000)


# ── the prepublic P0+P1 fix package (2026-10-23) ─────────────────────────
#
# Spec test items 1-11 (12 lives in tests/test_adjust_idempotency.py; 13-14
# are shopbot-side). Each protects a fix whose absence cost a real customer
# something: silence after a stuck order, a "free trial" lie on a metered
# service, a package buy capping an unlimited/payg service, a cleared rate
# stopping the meter, a suspended customer minting a second service, a
# two-commit meter window, a broken top-up promise, and the sub-min_topup
# doom loop.


def _stuck_order(session, uid: int, *, price: int = 30_000) -> ShopOrder:
    """An order the process died on: paid, provisioning, past the timeout."""
    from datetime import timedelta
    order = ShopOrder(shop_user_id=uid, data_limit_gb=10, duration_days=30, price=price,
                      status=ShopOrderStatus.provisioning,
                      created_at=utcnow() - timedelta(minutes=STUCK_ORDER_TIMEOUT_MINUTES + 1))
    session.add(order)
    session.flush()
    post_wallet_entry(session, uid, entry_type=ShopWalletEntryType.purchase,
                      amount=-price, order_id=order.id, commit=False)
    session.commit()
    return order


def test_sweep_late_delivery_pushes_once() -> None:
    print("\n[F1.1] a late-delivered stuck order gets the SAME push as an approve path, exactly once")
    from app import notify as notify_module

    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 100_000)

    qr_pushes: list[str] = []

    async def capture_photo(chat_id, photo, caption, *, filename="qr.png"):
        qr_pushes.append(caption)

    original = notify_module.send_photo_to_shop_user
    notify_module.send_photo_to_shop_user = capture_photo
    try:
        with Session(engine) as session:
            order = _stuck_order(session, uid)
            oid = order.id
            # The process died AFTER Marzban created the user: the sweep must
            # deliver it late AND tell the customer, who has heard nothing.
            fake.panel[f"shop{oid}"] = {
                "username": f"shop{oid}", "used_traffic": 0, "lifetime_used_traffic": 0,
                "data_limit": 10 * GB_BYTES, "expire": None, "status": "active",
                "note": f"shop order #{oid}", "subscription_url": f"/sub/tok_shop{oid}",
            }
            swept = asyncio.run(sweep_stuck_orders(session))
            check("one order swept", len(swept), 1)
            session.refresh(order)
            check("delivered late", order.status, ShopOrderStatus.delivered)
        check("the delivery push fired exactly once", len(qr_pushes), 1)

        # A sweep re-run must not double-push: the order left the sweep's
        # class (delivered) in the same session, so it is never visited again.
        with Session(engine) as session:
            again = asyncio.run(sweep_stuck_orders(session))
            check("re-run sweeps nothing", len(again), 0)
        check("never double-pushed", len(qr_pushes), 1)
    finally:
        notify_module.send_photo_to_shop_user = original


def test_sweep_refund_pushes_the_refund_notice() -> None:
    print("\n[F1.2] a sweep refund posts the refund entry AND tells the customer, with the reason")
    from app import notify as notify_module

    fake = FakeMarzban()  # empty panel -> the sweep refunds
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 100_000)

    pushes: list[str] = []

    async def capture(chat_id, text, reply_markup=None):
        pushes.append(text)

    original_text, original_admin = notify_module.send_to_shop_user, notify_module.notify_admin
    notify_module.send_to_shop_user = capture
    notify_module.notify_admin = capture
    try:
        with Session(engine) as session:
            order = _stuck_order(session, uid)
            oid = order.id
            asyncio.run(sweep_stuck_orders(session))
            session.refresh(order)
            check("order marked failed", order.status, ShopOrderStatus.failed)
            refunds = session.exec(select(ShopWalletEntry).where(
                ShopWalletEntry.order_id == oid,
                ShopWalletEntry.type == ShopWalletEntryType.refund,
            )).all()
            check("refund entry posted", len(refunds), 1)
            check("refund is the full price", refunds[0].amount, 30_000)
            check("money is back", wallet_balance(session, uid), 100_000)
        check("the customer was pushed exactly one notice", len(pushes), 1)
        body = pushes[0] if pushes else ""
        check("it names the amount back in the wallet", "۳۰,۰۰۰" in body and "برگشت" in body, True)
        check("it carries the reason", "علت" in body, True)
    finally:
        notify_module.send_to_shop_user, notify_module.notify_admin = original_text, original_admin


def test_sweep_refund_failure_tells_the_operator() -> None:
    print("\n[F1.3] when the refund notice cannot be sent, the operator is told to send it by hand")
    from app import notify as notify_module

    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 100_000)

    admin_told: list[str] = []

    async def broken(chat_id, text, reply_markup=None):
        raise RuntimeError("telegram down")

    async def admin_capture(text, reply_markup=None, *, thread_id=None):
        admin_told.append(text)

    original_text, original_admin = notify_module.send_to_shop_user, notify_module.notify_admin
    notify_module.send_to_shop_user = broken
    notify_module.notify_admin = admin_capture
    try:
        with Session(engine) as session:
            _stuck_order(session, uid)
            asyncio.run(sweep_stuck_orders(session))
    finally:
        notify_module.send_to_shop_user, notify_module.notify_admin = original_text, original_admin
    check("the refund still happened (best-effort push never blocks it)", True, True)
    check("the operator was told", any("دستی" in t for t in admin_told), True)


def test_payg_delivery_uses_payg_caption_not_trial() -> None:
    print("\n[F2.1] a payg service is NOT captioned as a free trial; a legacy trial still is")
    from app import notify as notify_module

    fake = FakeMarzban()
    client = _reset(fake)
    _make_user(client)
    _enable_payg(500)

    captions: list[str] = []

    async def capture_photo(chat_id, photo, caption, *, filename="qr.png"):
        captions.append(caption)

    original = notify_module.send_photo_to_shop_user
    notify_module.send_photo_to_shop_user = capture_photo
    try:
        r = client.post("/api/shop/bot/payg/start", headers=BOT_HEADERS, json={"telegram_id": 555})
        check("payg started", r.status_code, 200)
        check("payg caption", "مصرفی" in (captions[-1] if captions else ""), True)
        check("NOT the trial caption", "تست" in (captions[-1] if captions else ""), False)
        check("it warns the meter charges daily", "کم می‌شود" in captions[-1], True)

        # A LEGACY price-0 order with no payg account keeps the trial caption.
        captions.clear()
        client2 = _reset(fake)
        _make_user(client2, telegram_id=888)
        client2.patch("/api/shop/settings", json={"trial_enabled": True, "trial_gb": 1, "trial_hours": 6})
        t = client2.post("/api/shop/bot/trial", headers=BOT_HEADERS, json={"telegram_id": 888})
        check("trial granted", t.status_code, 200)
        check("trial caption survives", "تست" in (captions[-1] if captions else ""), True)
    finally:
        notify_module.send_photo_to_shop_user = original


def test_purchase_refused_on_payg_effective_account() -> None:
    print("\n[F3b.1] a package buy on a payg-effective service is refused BEFORE any money moves")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 100_000)
    _buy_from_wallet(client, 10)
    with Session(engine) as session:
        account = session.exec(select(Account)).first()
        account.billing_mode = BillingMode.payg  # operator flips the account to retail payg
        session.add(account)
        session.commit()
    with Session(engine) as session:
        balance_before = wallet_balance(session, uid)

    r = client.post("/api/shop/bot/purchase", headers=BOT_HEADERS,
                    json={"telegram_id": 555, "data_limit_gb": 10})
    check("refused (409)", r.status_code, 409)
    check("stable PAYG_TOPUP_INSTEAD prefix", "PAYG_TOPUP_INSTEAD" in r.json()["detail"], True)
    with Session(engine) as session:
        check("wallet untouched — refusal is pre-money", wallet_balance(session, uid), balance_before)
    check("no second account", len(fake.created), 1)

    q = client.post("/api/shop/bot/quote", headers=BOT_HEADERS,
                    json={"telegram_id": 555, "data_limit_gb": 10})
    check("quote refuses consistently (409)", q.status_code, 409)
    check("quote carries the same code", "PAYG_TOPUP_INSTEAD" in q.json()["detail"], True)

    o = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                    json={"telegram_id": 555, "data_limit_gb": 10})
    check("order flow refuses consistently (409)", o.status_code, 409)
    check("order flow carries the same code", "PAYG_TOPUP_INSTEAD" in o.json()["detail"], True)


def test_falsy_data_limit_is_unlimited() -> None:
    print("\n[F3a.1] a falsy data_limit (0) is treated as unlimited by both guards")
    fake = FakeMarzban()
    client = _reset(fake)
    uid = _make_user(client)
    _credit(uid, 200_000)
    _buy_from_wallet(client, 10)
    username = fake.created[0]

    # LOCAL mirror guard: 0 is how Marzban writes "unlimited" — a purchase
    # against it must be refused pre-money, not "extended" into a 10 GB cap.
    with Session(engine) as session:
        account = session.exec(select(Account)).first()
        account.data_limit = 0
        session.add(account)
        session.commit()
    r = client.post("/api/shop/bot/purchase", headers=BOT_HEADERS,
                    json={"telegram_id": 555, "data_limit_gb": 10})
    check("purchase refused (409)", r.status_code, 409)
    check("SERVICE_IS_UNLIMITED prefix", "SERVICE_IS_UNLIMITED" in r.json()["detail"], True)
    q = client.post("/api/shop/bot/quote", headers=BOT_HEADERS,
                    json={"telegram_id": 555, "data_limit_gb": 10})
    check("quote refused too", q.status_code, 409)

    # LIVE panel guard (defence in depth): mirror says 10 GB, the panel says 0
    # — the extension must refund, never cap the unlimited service.
    with Session(engine) as session:
        account = session.exec(select(Account)).first()
        account.data_limit = 10 * GB_BYTES
        session.add(account)
        session.commit()
    fake.panel[username]["data_limit"] = 0
    oid = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                      json={"telegram_id": 555, "data_limit_gb": 20}).json()["order_id"]
    pay = client.post(f"/api/shop/bot/orders/{oid}/pay", headers=BOT_HEADERS, json={"telegram_id": 555})
    check("the pay endpoint surfaces the failure (502)", pay.status_code, 502)
    with Session(engine) as session:
        order = session.get(ShopOrder, oid)
        check("order failed", order.status, ShopOrderStatus.failed)
        check("the wallet was refunded in full", wallet_balance(session, uid), 200_000 - 30_000)
    check("the panel's unlimited cap was never written over", fake.panel[username]["data_limit"], 0)


def test_cap_hit_skips_retail_payg_account() -> None:
    print("\n[F3c.1] the cap-hit settle skips a customer_id-NULL account: the wallet owns their money")
    from app import marzban_client as marzban_module
    from app.models import LedgerEntry
    from app.sync_job import _maybe_settle_payg_cap_hit

    fake = FakeMarzban()
    _reset(fake)
    resets: list[str] = []

    async def fake_reset(username):
        resets.append(username)
        return {"status": "active", "used_traffic": 0}

    original_reset = marzban_module.marzban_client.reset_user
    marzban_module.marzban_client.reset_user = fake_reset
    try:
        with Session(engine) as session:
            account = Account(marzban_username="shop_retail_cap", billing_mode=BillingMode.payg,
                              customer_id=None, group_id=None, status="limited",
                              data_limit=GB_BYTES, used_traffic=GB_BYTES)
            session.add(account)
            session.commit()
            asyncio.run(_maybe_settle_payg_cap_hit(session, account, utcnow()))
    finally:
        marzban_module.marzban_client.reset_user = original_reset
    check("no Marzban reset for a retail account", resets, [])
    with Session(engine) as session:
        from sqlmodel import select as _select
        check("no LedgerEntry row created",
              len(session.exec(_select(LedgerEntry)).all()), 0)


def test_settings_patch_rate_zero_guard() -> None:
    print("\n[F4.1] a cleared payg rate is refused while live retail-payg accounts exist; turning the offer off is not")
    fake = FakeMarzban()
    client = _reset(fake)

    ok_zero = client.patch("/api/shop/settings", json={"payg_rate_per_gb": 0})
    check("zero rate allowed with NO retail-payg accounts", ok_zero.status_code, 200)
    client.patch("/api/shop/settings", json={"payg_rate_per_gb": 500})

    # A live retail-payg account: started through the real flow.
    _make_user(client)
    client.patch("/api/shop/settings", json={"payg_enabled": True, "payg_rate_per_gb": 500})
    started = client.post("/api/shop/bot/payg/start", headers=BOT_HEADERS, json={"telegram_id": 555})
    check("payg account exists", started.status_code, 200)

    off = client.patch("/api/shop/settings", json={"payg_enabled": False})
    check("turning the OFFER off is legal", off.status_code, 200)

    zero = client.patch("/api/shop/settings", json={"payg_rate_per_gb": 0})
    check("zeroing the RATE is refused (400)", zero.status_code, 400)
    check("the reason names the live accounts", "metered" in zero.json()["detail"], True)
    none = client.patch("/api/shop/settings", json={"payg_rate_per_gb": None})
    check("clearing the RATE is refused too", none.status_code, 400)
    both = client.patch("/api/shop/settings", json={"payg_enabled": False, "payg_rate_per_gb": 0})
    check("refused regardless of payg_enabled in the same request", both.status_code, 400)
    off_again = client.patch("/api/shop/settings", json={"payg_enabled": False})
    check("payg_enabled=false alone still goes through", off_again.status_code, 200)


def test_suspended_payg_blocks_second_start() -> None:
    print("\n[F5.1] a suspended payg service blocks a second start; abandoned checkouts do not")
    fake = FakeMarzban()
    client = _reset(fake)
    _make_user(client)
    _enable_payg(500)
    started = client.post("/api/shop/bot/payg/start", headers=BOT_HEADERS, json={"telegram_id": 555})
    check("first start ok", started.status_code, 200)

    # The meter suspends it (empty wallet): status disabled + auto_suspended.
    with Session(engine) as session:
        account = session.exec(select(Account)).first()
        account.status = "disabled"
        account.auto_suspended = True
        session.add(account)
        session.commit()

    r = client.post("/api/shop/bot/payg/start", headers=BOT_HEADERS, json={"telegram_id": 555})
    check("suspended payg service still blocks (409)", r.status_code, 409)
    check("existing ALREADY_HAS_SERVICE refusal", "ALREADY_HAS_SERVICE" in r.json()["detail"], True)
    check("no second service was minted", len(fake.created), 1)

    # Abandoned checkouts must not lock anyone out: awaiting/failed orders
    # (no delivered order -> no service) do NOT block a start.
    fake2 = FakeMarzban()
    client2 = _reset(fake2)
    uid2 = _make_user(client2, telegram_id=777)
    _enable_payg(500)
    oid = client2.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                       json={"telegram_id": 777, "data_limit_gb": 10}).json()["order_id"]
    with Session(engine) as session:
        abandoned = session.get(ShopOrder, oid)
        abandoned.status = ShopOrderStatus.failed  # a checkout that went nowhere
        abandoned.error = "never provisioned"
        session.add(abandoned)
        session.commit()
    r2 = client2.post("/api/shop/bot/payg/start", headers=BOT_HEADERS, json={"telegram_id": 777})
    check("awaiting/failed orders do not block", r2.status_code, 200)
    check("exactly one service for them", len(fake2.created), 1)


def test_meter_debit_and_baseline_atomic() -> None:
    print("\n[F6.1] the meter's wallet debit and baseline roll commit together — or not at all")
    import app.shop_service as shop_service_module
    from app.shop_service import _sweep_payg_accounts

    fake = FakeMarzban()
    client = _reset(fake)
    _make_user(client)
    _enable_payg(500)
    client.post("/api/shop/bot/payg/start", headers=BOT_HEADERS, json={"telegram_id": 555})
    with Session(engine) as session:
        uid = _shop_user_id(session)
    _credit(uid, 1000)
    with Session(engine) as session:
        account = session.exec(select(Account)).first()
        account.used_traffic = GB_BYTES
        session.add(account)
        session.commit()

    original = shop_service_module.post_wallet_entry

    def crash_between(*args, **kwargs):
        kwargs["commit"] = False
        entry = original(*args, **kwargs)
        raise RuntimeError(f"crash right after wallet entry #{entry.id}")

    shop_service_module.post_wallet_entry = crash_between
    crashed = False
    try:
        with Session(engine) as session:
            try:
                asyncio.run(_sweep_payg_accounts(session))
            except RuntimeError:
                crashed = True
    finally:
        shop_service_module.post_wallet_entry = original
    check("the injected crash really happened", crashed, True)

    with Session(engine) as session:
        entries = session.exec(select(ShopWalletEntry).where(
            ShopWalletEntry.shop_user_id == uid,
            ShopWalletEntry.type == ShopWalletEntryType.purchase,
        )).all()
        account = session.exec(select(Account)).first()
    check("a crash between them leaves NO debit", len(entries), 0)
    check("...and NO baseline roll", account.usage_baseline, 0)

    # The healthy path commits both, and carries the ride-alongs: the meter
    # note is «بیل مصرف روزانه» and no start-order id is stamped on it.
    with Session(engine) as session:
        result = asyncio.run(_sweep_payg_accounts(session))
    check("charged once", result["charged"], 1)
    with Session(engine) as session:
        entry = session.exec(select(ShopWalletEntry).where(
            ShopWalletEntry.shop_user_id == uid,
            ShopWalletEntry.type == ShopWalletEntryType.purchase,
        )).one()
        account = session.exec(select(Account)).first()
    check("entry and baseline landed together", (entry.amount, account.usage_baseline),
          (-500, GB_BYTES))
    check("meter note is «بیل مصرف روزانه»", entry.note.startswith("بیل مصرف روزانه"), True)
    check("meter entry carries no start-order id", entry.order_id, None)


def test_reactivation_fires_from_topup_approve() -> None:
    print("\n[F7.1] approving a top-up re-enables a funded payg service — once, and only if suspended")
    fake = FakeMarzban()
    client = _reset(fake)
    _make_user(client)
    _enable_payg(500)
    client.post("/api/shop/bot/payg/start", headers=BOT_HEADERS, json={"telegram_id": 555})
    username = fake.created[0]
    with Session(engine) as session:
        uid = _shop_user_id(session)
    _credit(uid, 500)  # less than two GB's worth
    with Session(engine) as session:
        account = session.exec(select(Account)).first()
        account.used_traffic = 2 * GB_BYTES  # 2 GB at 500 T/GB = 1000 T -> balance -500
        session.add(account)
        session.commit()
    from app.shop_service import _sweep_payg_accounts
    with Session(engine) as session:
        asyncio.run(_sweep_payg_accounts(session))
    with Session(engine) as session:
        account = session.exec(select(Account)).first()
    check("suspended by the meter", account.auto_suspended, True)
    modifies_after_suspend = len(fake.modified)

    # 50_000, not the wallet's shortfall: F10 refuses a PLAIN top-up under
    # min_topup (10,000 here), and this test is about reactivation, not the
    # minimum — the credit just has to clear the approval path.
    topup = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                        json={"telegram_id": 555, "claimed_amount": 50_000, "receipt_file_id": "r1"}).json()
    approved = client.post(f"/api/shop/topups/{topup['id']}/approve", json={})
    check("approval ok", approved.status_code, 200)
    with Session(engine) as session:
        account = session.exec(select(Account)).first()
        check("auto_suspended cleared", account.auto_suspended, False)
        check("account active again", account.status, "active")
    check("Marzban enable really called once", len(fake.modified), modifies_after_suspend + 1)
    check("panel account is active", fake.panel[username]["status"], "active")

    # The guard: a SECOND approval (wallet already positive, not suspended)
    # must not call Marzban again — never double-enable.
    topup2 = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                         json={"telegram_id": 555, "claimed_amount": 50_000, "receipt_file_id": "r2"}).json()
    client.post(f"/api/shop/topups/{topup2['id']}/approve", json={})
    check("second approval re-enacts nothing", len(fake.modified), modifies_after_suspend + 1)

    # And a customer whose account is NOT suspended sees no reactivation at all.
    fake2 = FakeMarzban()
    client2 = _reset(fake2)
    _make_user(client2, telegram_id=777)
    t2 = client2.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                      json={"telegram_id": 777, "claimed_amount": 50_000, "receipt_file_id": "r3"}).json()
    approved2 = client2.post(f"/api/shop/topups/{t2['id']}/approve", json={})
    check("plain approval ok", approved2.status_code, 200)
    check("no reactivation calls for a non-suspended customer", len(fake2.modified), 0)


def test_order_bound_topup_skips_min_topup() -> None:
    print("\n[F10.1] an order-bound receipt is an exact payment — no minimum; a plain top-up keeps it")
    fake = FakeMarzban()
    client = _reset(fake)  # min_topup=10000, max_topup=50_000_000
    _make_user(client)

    oid = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                      json={"telegram_id": 555, "data_limit_gb": 10}).json()["order_id"]
    bound = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                        json={"telegram_id": 555, "claimed_amount": 3_000, "order_id": oid})
    check("order-bound topup under min_topup is accepted", bound.status_code, 200)

    plain = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                        json={"telegram_id": 555, "claimed_amount": 3_000})
    check("plain topup under min_topup is still refused (400)", plain.status_code, 400)

    oid2 = client.post("/api/shop/bot/orders", headers=BOT_HEADERS,
                       json={"telegram_id": 555, "data_limit_gb": 10}).json()["order_id"]
    huge = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                       json={"telegram_id": 555, "claimed_amount": 60_000_000, "order_id": oid2})
    check("max_topup still caps an order-bound topup", huge.status_code, 400)


def test_overdue_apology_reaches_typed_receipts() -> None:
    print("\n[ride-along] the late-payment apology fires for TYPED receipts too, not only photos")
    from datetime import timedelta
    from app import notify as notify_module
    from app.shop_service import notify_overdue_payments

    fake = FakeMarzban()
    client = _reset(fake)
    _make_user(client)
    tid = client.post("/api/shop/bot/topups", headers=BOT_HEADERS,
                      json={"telegram_id": 555, "claimed_amount": 50_000,
                            "receipt_text": "TRACK123"}).json()["id"]

    sent: list[str] = []

    async def capture(chat_id, text, reply_markup=None):
        sent.append(text)

    async def admin_capture(text, reply_markup=None, *, thread_id=None):
        sent.append(text)

    original_text, original_admin = notify_module.send_to_shop_user, notify_module.notify_admin
    notify_module.send_to_shop_user = capture
    notify_module.notify_admin = admin_capture
    try:
        with Session(engine) as session:
            check("not late yet", asyncio.run(notify_overdue_payments(session)), 0)
            topup = session.get(ShopTopup, tid)
            topup.created_at = utcnow() - timedelta(minutes=45)
            session.add(topup)
            session.commit()
            check("typed receipt past the promise: announced", asyncio.run(notify_overdue_payments(session)), 1)
            check("never twice", asyncio.run(notify_overdue_payments(session)), 0)
    finally:
        notify_module.send_to_shop_user, notify_module.notify_admin = original_text, original_admin
    check("the apology actually went out", len(sent) >= 1, True)


if __name__ == "__main__":
    raise SystemExit(main())
