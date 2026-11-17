"""PDF receipts are accepted (2026-11-18): banking apps export the receipt
as a PDF, and the old flow bounced it with «این فایل را نمی‌توانم باز کنم» —
a customer forced to re-screenshot at the most anxious moment of the
product (T0 #7, B-3 #3).

  * application/pdf (and a PDF filename on an opaque mime) goes through the
    same _submit_receipt path as a photo — same pending-order recovery, same
    one-at-a-time guard, same confirmation.
  * The customer-facing prompts name PDF among the accepted forms.
  * Other file types still get the honest «این فرمت را نمی‌توانم باز کنم».

The backend relay renders the PDF as a DOCUMENT for the operator (see
tests/test_pdf_receipt_relay.py) — nothing here changes.

Run (from shopbot/):  ../bot/venv/Scripts/python test_pdf_receipt.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("SHOP_BOT_TOKEN", "")
os.environ.setdefault("SHOP_BOT_API_KEY", "k")
os.environ.setdefault("API_BASE_URL", "https://panel.test")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import texts  # noqa: E402
import handlers.shop as shop  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


class StubMessage:
    def __init__(self, document=None):
        self.document = document
        self.replies: list[tuple] = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append((text, reply_markup))


def make_doc(mime: str | None, file_id: str = "f1", file_name: str | None = None):
    return SimpleNamespace(mime_type=mime, file_id=file_id, file_name=file_name)


def make_update(document=None):
    msg = StubMessage(document)
    upd = SimpleNamespace(
        effective_message=msg, message=msg,
        effective_user=SimpleNamespace(id=555, username="ali_t", first_name="علی", last_name=None),
    )
    return (upd, SimpleNamespace(args=[], user_data={}, chat_data={})), msg


SESSION = {
    "balance": 0, "trial_available": False, "shop_name": None, "support_handle": None,
    "is_blocked": False, "is_open": True,
    "min_topup": 50_000, "max_topup": 5_000_000, "card_number": "6037", "card_holder": "x",
    "approval_eta_minutes": 30, "price_per_gb": 5_000, "min_gb": 5, "max_gb": 100,
    "plan_duration_days": 30, "ledger_debt": 0, "debt_breakdown": [], "debt_exact": True,
    "payg_enabled": False, "payg_rate_per_gb": 0, "has_accounts": True, "has_payg_service": False,
    "phone": None,
}
ORDER = {
    "order_id": 77, "creates_new": True, "data_limit_gb": 10,
    "duration_days": 30, "price": 100_000, "balance": 0,
    "shortfall": 100_000, "payable_from_wallet": False,
    "card_number": "6037", "card_holder": "x", "approval_eta_minutes": 30,
}
STATE = {"session": dict(SESSION), "topups": []}


async def fake_session(update) -> dict:
    return dict(STATE["session"])


class StubBackend:
    async def get(self, path, params=None):
        if path == "/api/shop/bot/orders/pending":
            return None
        raise AssertionError(path)

    async def post(self, path, json=None, timeout: float = 20):
        if path == "/api/shop/bot/orders":
            return dict(ORDER)
        if path == "/api/shop/bot/topups":
            STATE["topups"].append(dict(json))
            return {"reference_code": "ABCD", "status": "pending"}
        raise AssertionError(path)


shop.backend = StubBackend()
shop._session = fake_session  # type: ignore[assignment]


async def enter_receipt_wait(msg, ctx):
    """The buy flow up to the receipt-await state (menu tap → volume tap)."""
    msg.text = texts.MENU_BUY
    await shop.handle_text(msg.parent[0], msg.parent[1])
    msg.text = "10"
    await shop.handle_text(msg.parent[0], msg.parent[1])
    assert ctx.user_data.get(shop._STATE) == shop._STATE_AWAITING_RECEIPT


async def main() -> None:
    # ── 1. a PDF document in the receipt wait → submitted like a photo ────
    (upd, ctx), msg = make_update()
    msg.parent = (upd, ctx)
    await enter_receipt_wait(msg, ctx)
    STATE["topups"].clear()
    msg.document = make_doc("application/pdf", file_id="pdf1")
    await shop.handle_document(upd, ctx)
    check("application/pdf → receipt submitted with its file_id",
          len(STATE["topups"]) == 1 and STATE["topups"][0].get("receipt_file_id") == "pdf1",
          repr(STATE["topups"]))
    check("application/pdf → the submitted confirmation (not the bounce)",
          any("دریافت شد" in r[0] for r in msg.replies)
          and not any("نمی‌توانم باز" in r[0] for r in msg.replies),
          repr([r[0] for r in msg.replies]))

    # ── 2. opaque mime but a .pdf filename → also accepted ────────────────
    (upd, ctx), msg = make_update()
    msg.parent = (upd, ctx)
    await enter_receipt_wait(msg, ctx)
    STATE["topups"].clear()
    msg.document = make_doc("application/octet-stream", file_id="pdf2", file_name="receipt.PDF")
    await shop.handle_document(upd, ctx)
    check("a .pdf filename on an opaque mime → accepted",
          len(STATE["topups"]) == 1 and STATE["topups"][0].get("receipt_file_id") == "pdf2",
          repr(STATE["topups"]))

    # ── 3. outside any flow, cold: a PDF still finds its pending order ────
    STATE["topups"].clear()
    STATE["pending_order"] = {"order_id": 88, "shortfall": 90_000, "data_limit_gb": 30}
    STATE["pending"] = True

    async def fake_session2(update) -> dict:
        return dict(STATE["session"])

    class StubBackend2(StubBackend):
        async def get(self, path, params=None):
            if path == "/api/shop/bot/orders/pending":
                if STATE["pending"]:
                    return {"order_id": 88, "shortfall": 90_000, "data_limit_gb": 30}
                return None
            raise AssertionError(path)

    shop.backend = StubBackend2()
    (upd, ctx), msg = make_update()
    msg.document = make_doc("application/pdf", file_id="pdf3")
    await shop.handle_document(upd, ctx)
    check("cold PDF after a VPN-off payment → recovered onto the pending order",
          len(STATE["topups"]) == 1 and STATE["topups"][0].get("order_id") == 88,
          repr(STATE["topups"]))
    shop.backend = StubBackend()

    # ── 4. genuinely unsupported file still bounces honestly ──────────────
    (upd, ctx), msg = make_update(make_doc("application/zip", file_id="z1"))
    await shop.handle_document(upd, ctx)
    check("a zip still gets the honest bounce",
          any("نمی‌توانم باز" in r[0] for r in msg.replies), repr([r[0] for r in msg.replies]))

    # ── 5. image-as-document keeps working ────────────────────────────────
    STATE["topups"].clear()
    (upd, ctx), msg = make_update()
    msg.parent = (upd, ctx)
    await enter_receipt_wait(msg, ctx)
    msg.document = make_doc("image/png", file_id="img1")
    await shop.handle_document(upd, ctx)
    check("image-as-document still submits",
          len(STATE["topups"]) == 1 and STATE["topups"][0].get("receipt_file_id") == "img1",
          repr(STATE["topups"]))

    # ── 6. the prompts name PDF among the accepted forms ──────────────────
    for label, out in (
        ("topup_instructions", texts.topup_instructions(100_000, "6037", None, 30)),
        ("topup_instructions_with_minimum",
         texts.topup_instructions_with_minimum(50_000, 30_000, "6037", None, 30)),
        ("debt_receipt_instructions", texts.debt_receipt_instructions(100_000, "6037", None, 30)),
        ("RECEIPT_AS_FILE", texts.RECEIPT_AS_FILE),
        ("NEED_PHOTO", texts.NEED_PHOTO),
        ("receipt_failed", texts.receipt_failed(None)),
        ("help_text", texts.help_text(10_000, 30, 30, None)),
    ):
        check(f"{label} names PDF", "PDF" in out, repr(out))


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All pdf-receipt cases passed.")
