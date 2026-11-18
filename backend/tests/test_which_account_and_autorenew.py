"""Round F (2026-11-18, owner): «الان من اگه بزنم مثلاً ۱۰ گیگ کدوم اکانتم
شارژ میشه؟» — the buy path must NAME the account a plain purchase extends,
everywhere it matters:

  * the SESSION carries renewable_label (the customer's own nickname, or the
    panel username as the fallback identity) so the VOLUME screen can say
    which service the buy adds to;
  * the /orders INTENT carries target_label for plain buys too (it only did
    for per-account renew:) — so the PAYMENT screen shows «🔄 تمدید «X»»;
  * the ACCOUNTS rows carry auto_renew_enabled, and a customer-side toggle
    endpoint flips it (until today only the operator could — the customer
    could neither turn the standing auto-renew ON nor OFF).

Plain `python -m tests.test_which_account_and_autorenew` from `backend/`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="which_acct_test_")) / "test.db"
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
from sqlmodel import Session  # noqa: E402

from app.auth import require_auth  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    ShopOrder,
    ShopOrderStatus,
    ShopSettings,
    ShopUser,
    utcnow,
)

init_db()

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


app.dependency_overrides[require_auth] = lambda: "test-admin"
client = TestClient(app)
BOT = {"X-Shop-Bot-Key": "test-shop-key"}
GB = 1024 ** 3


def _session(telegram_id: int) -> dict:
    r = client.post("/api/shop/bot/session", headers=BOT, json={"telegram_id": telegram_id})
    assert r.status_code == 200, r.text
    return r.json()


def _seed(*, tg: int, username: str, nick: str | None, disabled: bool = False) -> int:
    """A shop user + a delivered order on one live account."""
    with Session(engine) as session:
        user = ShopUser(telegram_id=tg, display_name=f"u{tg}")
        session.add(user)
        session.commit()
        session.refresh(user)
        account = Account(marzban_username=username, data_limit=30 * GB,
                          used_traffic=5 * GB, expire=int(utcnow().timestamp()) + 20 * 86400,
                          status="disabled" if disabled else "active",
                          service_name=nick)
        session.add(account)
        session.commit()
        session.refresh(account)
        order = ShopOrder(shop_user_id=user.id, data_limit_gb=30, duration_days=30,
                          price=150_000, status=ShopOrderStatus.delivered,
                          account_id=account.id, marzban_username=username)
        session.add(order)
        session.commit()
        return account.id


def main() -> None:
    with Session(engine) as session:
        st = session.get(ShopSettings, 1)
        if st is None:
            st = ShopSettings(id=1)
        st.is_open = True
        st.price_per_gb = 5_000
        st.card_number = "6037"
        session.add(st)
        session.commit()

    # ── 1. the SESSION names the renewable service ────────────────────────
    _seed(tg=801, username="shop-ali", nick="گوشی علی")
    s = _session(801)
    check("session carries renewable_label = the customer's nickname",
          s.get("renewable_label") == "گوشی علی", repr(s.get("renewable_label")))

    _seed(tg=802, username="shop-unnamed", nick=None)
    s = _session(802)
    check("no nickname → the panel username is the fallback label",
          s.get("renewable_label") == "shop-unnamed", repr(s.get("renewable_label")))

    s = _session(803)
    check("a newcomer's session has NO renewable label",
          s.get("renewable_label") in (None, ""), repr(s.get("renewable_label")))

    _seed(tg=804, username="shop-off", nick="خاموش", disabled=True)
    s = _session(804)
    check("a DISABLED-only holder gets no extend promise",
          s.get("renewable_label") in (None, ""), repr(s.get("renewable_label")))

    # ── 2. the plain-buy INTENT names the account it will extend ──────────
    _seed(tg=805, username="shop-intent", nick="گوشی مامان")
    r = client.post("/api/shop/bot/orders", headers=BOT,
                    json={"telegram_id": 805, "data_limit_gb": 10})
    check("plain-buy intent → 200", r.status_code == 200, repr(r.text))
    body = r.json()
    check("intent target_label names the extended service",
          body.get("target_label") == "گوشی مامان", repr(body.get("target_label")))

    r = client.post("/api/shop/bot/orders", headers=BOT,
                    json={"telegram_id": 805, "data_limit_gb": 10, "force_new_service": True})
    check("force_new intent → NO extend promise",
          r.json().get("target_label") is None, repr(r.json().get("target_label")))

    r = client.post("/api/shop/bot/orders", headers=BOT,
                    json={"telegram_id": 803, "data_limit_gb": 10})
    check("newcomer intent → no extend promise",
          r.json().get("target_label") is None, repr(r.json().get("target_label")))

    # ── 3. the rows carry the standing auto-renew state ───────────────────
    with Session(engine) as session:
        from sqlmodel import select
        from app.models import Account as A
        acct = session.exec(select(A).where(A.marzban_username == "shop-ali")).first()
        acct.auto_renew_enabled = True
        session.add(acct)
        session.commit()
        account_id = acct.id  # capture INSIDE the session (detached afterwards)
    rows = client.get("/api/shop/bot/accounts", headers=BOT,
                      params={"telegram_id": 801}).json()
    check("accounts rows carry auto_renew_enabled",
          rows and rows[0].get("auto_renew_enabled") is True, repr(rows[:1]))

    # ── 4. the customer-side toggle ────────────────────────────────────────
    r = client.post(f"/api/shop/bot/accounts/{account_id}/auto-renew-toggle", headers=BOT,
                    json={"telegram_id": 801, "enabled": False})
    check("toggle OFF → 200", r.status_code == 200, repr(r.text))
    check("toggle response reports the new state",
          r.json().get("auto_renew_enabled") is False, repr(r.text))
    rows = client.get("/api/shop/bot/accounts", headers=BOT,
                      params={"telegram_id": 801}).json()
    check("the list reflects the OFF state",
          rows[0].get("auto_renew_enabled") is False, repr(rows[:1]))

    r = client.post(f"/api/shop/bot/accounts/{account_id}/auto-renew-toggle", headers=BOT,
                    json={"telegram_id": 999, "enabled": True})
    check("someone else's account → 404, no oracle", r.status_code == 404, repr(r.text))

    r = client.post(f"/api/shop/bot/accounts/{account_id}/auto-renew-toggle", headers=BOT,
                    json={"telegram_id": 801, "enabled": True})
    check("toggle back ON → 200 True", r.json().get("auto_renew_enabled") is True, repr(r.text))


main()

print()
if failures:
    print(f"RESULT: {len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("RESULT: which-account + autorenew-toggle checks OK")
