"""Round B (2026-11-17): the backend's PUSH copy must speak the vocabulary
the bot side already settled (T5/R2).

The R2 Persian pass fixed shopbot/texts.py only; backend/app/shop_texts.py
kept the retired words («بدهیِ صورت‌حسابی», «بدهی تسویه‌نشده», «صورت‌حساب»),
the missing ZWNJ («همینجا»), the typo «کسرشی», and instructions naming
«🛒 خرید سرویس» on keyboards that render «🛒 خرید / تمدید سرویس» (every
account-holder's — the expiry/usage/trial-ending pushes go ONLY to
account-holders). These pushes land when no conversation is open — exactly
the moments the customer most needs an instruction they can follow.

Also pinned: the two pay-debt-from-wallet refusals the shopbot relays
verbatim (R1-3) now live HERE as constants (one home), and the warning
sweep's inline renew button names the action the tap actually performs.

Plain `python -m tests.test_shop_texts_copy` from `backend/`.
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="shop_copy_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""
os.environ["SHOP_BOT_API_KEY"] = "test-shop-key"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import shop_texts as st  # noqa: E402
from app.routers import shop as shop_router  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


def main() -> None:
    # ── 1. the pay-debt refusals live here, in the settled vocabulary ─────
    check("PAY_DEBT_NOTHING_TO_PAY exists, says ماندهٔ پرداخت‌نشده",
          isinstance(getattr(st, "PAY_DEBT_NOTHING_TO_PAY", None), str)
          and "ماندهٔ پرداخت‌نشده" in st.PAY_DEBT_NOTHING_TO_PAY
          and "بدهی" not in st.PAY_DEBT_NOTHING_TO_PAY,
          repr(getattr(st, "PAY_DEBT_NOTHING_TO_PAY", "<missing>")))
    check("PAY_DEBT_NONE_DETAIL exists, same vocabulary, distinct sentence",
          isinstance(getattr(st, "PAY_DEBT_NONE_DETAIL", None), str)
          and "ماندهٔ پرداخت‌نشده" in st.PAY_DEBT_NONE_DETAIL
          and st.PAY_DEBT_NONE_DETAIL != st.PAY_DEBT_NOTHING_TO_PAY,
          repr(getattr(st, "PAY_DEBT_NONE_DETAIL", "<missing>")))

    # ── 2. button references match the keyboard the receiver renders ──────
    for fn, label in ((st.expiring_soon, "expiring_soon"), (st.trial_ending, "trial_ending")):
        out = fn(10, 3, None) if fn is st.expiring_soon else fn(12, None)
        check(f"{label}: names the «خرید / تمدید» shape its receiver has",
              "🛒 خرید / تمدید سرویس" in out and "🛒 خرید سرویس" not in out, repr(out))
    plain = st.topup_approved_plain(50_000, 50_000, None, has_accounts=True)
    check("topup_approved_plain(has_accounts) names the renew shape",
          "🛒 خرید / تمدید سرویس" in plain and "🛒 خرید سرویس" not in plain, repr(plain))
    plain_new = st.topup_approved_plain(50_000, 50_000, None, has_accounts=False)
    check("topup_approved_plain(newcomer) names the plain shape",
          "🛒 خرید سرویس" in plain_new and "تمدید" not in plain_new, repr(plain_new))
    no_svc = st.topup_approved_no_service(50_000, 50_000, None, has_accounts=True)
    check("topup_approved_no_service(has_accounts) names the renew shape",
          "🛒 خرید / تمدید سرویس" in no_svc and "🛒 خرید سرویس" not in no_svc, repr(no_svc))

    # ── 3. retired vocabulary is gone from the push texts ────────────────
    refunded = st.order_refunded(100_000)
    check("order_refunded: «کسرشی» typo gone",
          "کسرشی" not in refunded and "هیچ کسری نشده" in refunded, repr(refunded))
    reminder = st.debt_reminder(120_000, 3)
    check("debt_reminder: hamze form «پرداخت‌نشدهٔ», no «-ی»",
          "ماندهٔ پرداخت‌نشدهٔ شما" in reminder and "پرداخت‌نشده‌ی" not in reminder,
          repr(reminder))
    suspended = st.debt_suspended(120_000)
    check("debt_suspended: «ماندهٔ پرداخت‌نشدهٔ», no «بدهی تسویه‌نشده»",
          "ماندهٔ پرداخت‌نشدهٔ" in suspended and "بدهی تسویه‌نشده" not in suspended,
          repr(suspended))

    # ── 4. the ZWNJ pass reaches the push side too ────────────────────────
    short = st.topup_approved_short(50_000, 0, 90_000, "6037-9911-2222-3333", None, None)
    check("topup_approved_short: «همین‌جا» with ZWNJ",
          "همین‌جا" in short and "همینجا" not in short, repr(short))
    overdue = st.payment_overdue("ABCD", None)
    check("payment_overdue: «همین‌جا» with ZWNJ",
          "همین‌جا" in overdue and "همینجا" not in overdue, repr(overdue))

    # ── 5. the sources hold no retired copy anywhere ──────────────────────
    for mod in (st, shop_router):
        src = inspect.getsource(mod)
        check(f"{mod.__name__}: no retired «بدهیِ صورت‌حسابی»",
              "بدهیِ صورت‌حسابی" not in src)
    st_src = inspect.getsource(st)
    check("shop_texts: no «کسرشی», no bare «همینجا»",
          "کسرشی" not in st_src and "همینجا" not in st_src)
    from app import shop_service  # noqa: E402  (needs the env boilerplate above)
    svc_src = inspect.getsource(shop_service)
    check("shop_service: warning buttons carry no inline copy",
          "تمدید حجم دلخواه" not in svc_src and "🛒 خرید سرویس" not in svc_src)
    check("shop_service: warning renew button uses the shared constant",
          "shop_texts.WARNING_RENEW_BUTTON" in svc_src
          and "shop_texts.MENU_BUY_LABEL" in svc_src)

    # ── 6. the label constants mirror the bot's menu, byte for byte ──────
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shopbot"))
    import texts as bot_texts  # noqa: E402
    check("MENU_BUY_LABEL == shopbot MENU_BUY",
          st.MENU_BUY_LABEL == bot_texts.MENU_BUY, repr(st.MENU_BUY_LABEL))
    check("MENU_BUY_RENEW_LABEL == shopbot MENU_BUY_RENEW",
          st.MENU_BUY_RENEW_LABEL == bot_texts.MENU_BUY_RENEW,
          repr(st.MENU_BUY_RENEW_LABEL))


main()

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All shop-copy cases passed.")
