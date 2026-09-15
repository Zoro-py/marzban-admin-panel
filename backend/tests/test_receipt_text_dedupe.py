"""Checks for the new typed-receipt path: create_topup(receipt_text=...) and
find_prior_receipt_text_use().

Plain `python -m tests.test_receipt_text_dedupe` from `backend/`, same
harness shape as tests/test_shop.py.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="receipt_text_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "test")
os.environ.setdefault("MARZBAN_PASSWORD", "test")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ADMIN_CHAT_ID"] = ""
os.environ["SHOP_BOT_TOKEN"] = ""
os.environ["SHOP_BOT_API_KEY"] = "test-shop-key"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app.models import ShopUser  # noqa: E402
from app.shop_service import create_topup, find_prior_receipt_text_use  # noqa: E402

init_db()

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


with Session(engine) as session:
    user_a = ShopUser(telegram_id=1001)
    user_b = ShopUser(telegram_id=1002)
    session.add(user_a)
    session.add(user_b)
    session.commit()
    session.refresh(user_a)
    session.refresh(user_b)

    # No prior use for a fresh code.
    check(
        "fresh receipt_text has no prior use",
        find_prior_receipt_text_use(session, "TRX-000111") is None,
    )

    topup1 = create_topup(session, user_a, 50_000, receipt_file_id=None, receipt_text="TRX-000111")
    check("topup1 stored the receipt_text", topup1.receipt_text == "TRX-000111")

    # Same code, different user -> flagged as reused.
    reused = find_prior_receipt_text_use(session, "TRX-000111")
    check("exact reuse detected", reused is not None and reused.id == topup1.id)

    # Case-insensitive / whitespace-insensitive match.
    reused_ci = find_prior_receipt_text_use(session, "  trx-000111  ")
    check("case/whitespace-insensitive match", reused_ci is not None and reused_ci.id == topup1.id)

    # A different code is not flagged.
    check(
        "different code not flagged",
        find_prior_receipt_text_use(session, "TRX-999999") is None,
    )

    # exclude_topup_id excludes the topup's own row (self-match shouldn't count).
    check(
        "exclude_topup_id excludes self",
        find_prior_receipt_text_use(session, "TRX-000111", exclude_topup_id=topup1.id) is None,
    )

    # A second, different customer can create a topup reusing the same text —
    # create_topup itself doesn't block on this (the operator decides), only
    # the caller/router layer flags it.
    topup2 = create_topup(session, user_b, 20_000, receipt_file_id=None, receipt_text="TRX-000111")
    check("second topup with same text still created", topup2.id != topup1.id)

    # None / empty receipt_text never matches anything.
    check("None receipt_text returns None", find_prior_receipt_text_use(session, None) is None)
    check("empty-string receipt_text returns None", find_prior_receipt_text_use(session, "   ") is None)

    # A topup created without receipt_text (photo-based) doesn't interfere.
    topup3 = create_topup(session, user_a, 10_000, receipt_file_id="some_file_id")
    check("photo-only topup has no receipt_text", topup3.receipt_text is None)
    check(
        "photo-only topup not matched by unrelated text lookup",
        find_prior_receipt_text_use(session, "TRX-000111", exclude_topup_id=topup3.id) is not None,
    )

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All receipt_text dedupe cases passed.")
