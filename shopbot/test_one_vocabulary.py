"""T5 one-vocabulary regression tests (feat/shopbot-ux-rebuild):

The customer reads ONE money vocabulary everywhere — «موجودی» (their
credit), «ماندهٔ پرداخت‌نشده» (what they owe), «شارژ» (adding money).
«کیف پول» is retired from customer-facing copy (the owner's principle:
one balance, in words a customer uses); it survives ONLY in the LEGACY
menu labels old chats still show. «قابل خرید (کیف پول منهای بدهی)»-style
arithmetic and the ⚠️ alarm on the approximate notice are gone.

Run (from shopbot/):
  ../bot/venv/Scripts/python test_one_vocabulary.py
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import sys
from pathlib import Path

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


BASE = Path(__file__).resolve().parent
BACKEND_TEXTS = BASE.parent / "backend" / "app" / "shop_texts.py"


def source_lines(path: Path) -> list[str]:
    return io.open(path, encoding="utf-8").read().splitlines()


async def main() -> None:
    # ── 1. «کیف پول» survives only in the LEGACY menu labels ─────────────
    bot_texts = source_lines(BASE / "texts.py")
    allowed = ("MENU_WALLET", "MENU_TOPUP", "MENU_PAY_DEBT")
    offenders = []
    for i, line in enumerate(bot_texts, 1):
        if "کیف پول" in line and not any(name in line for name in allowed):
            offenders.append(f"texts.py:{i}: {line.strip()[:80]}")
    check("texts.py: «کیف پول» only inside the legacy menu labels",
          not offenders, repr(offenders))

    shop_src = source_lines(BASE / "handlers" / "shop.py")
    offenders = [f"shop.py:{i}" for i, line in enumerate(shop_src, 1) if "کیف پول" in line]
    check("handlers/shop.py: no «کیف پول» anywhere (labels, views, prompts)",
          not offenders, repr(offenders))

    backend_src = source_lines(BACKEND_TEXTS)
    offenders = [f"shop_texts.py:{i}" for i, line in enumerate(backend_src, 1) if "کیف پول" in line]
    check("backend shop_texts.py: no «کیف پول» in any customer push",
          not offenders, repr(offenders))

    # ── 2. the جزئیات view: the debt headline, no arithmetic ─────────────
    detail = texts.debt_status(123_000, [{"name": "سید", "debt": 123000}], True)
    check("debt_status leads with «ماندهٔ پرداخت‌نشده»",
          "ماندهٔ پرداخت‌نشده" in detail, repr(detail))
    check("debt_status has NO «قابل خرید» arithmetic line",
          "قابل خرید" not in detail, repr(detail))
    check("debt_status keeps the per-account breakdown (it lives behind جزئیات)",
          "سید" in detail, repr(detail))

    # ── 3. the approximate notice: short, calm, no alarm mark ────────────
    check("DEBT_NOT_EXACT is calm (no ⚠️) and short (≤140 chars)",
          "⚠️" not in texts.DEBT_NOT_EXACT and len(texts.DEBT_NOT_EXACT) <= 140,
          repr(texts.DEBT_NOT_EXACT))
    check("DEBT_NOT_EXACT says it is approximate and names the way out",
          "تقریبی" in texts.DEBT_NOT_EXACT and "پیام" in texts.DEBT_NOT_EXACT,
          repr(texts.DEBT_NOT_EXACT))

    # ── 4. the suspended badge: one reason, the one action ───────────────
    check("STATUS_SUSPENDED_BADGE offers شارژ and drops the debt framing",
          "شارژ" in texts.STATUS_SUSPENDED_BADGE and "بدهی" not in texts.STATUS_SUSPENDED_BADGE,
          repr(texts.STATUS_SUSPENDED_BADGE))

    # ── 5. the wallet-history labels speak the same language ─────────────
    check("topup entry labels «شارژ»", shop._wallet_label({"type": "topup", "note": None}) == "شارژ")
    check("meter entry labels «مصرف روزانه»",
          shop._wallet_label({"type": "purchase", "note": "Payg meter: 0.1 GB"}) == "مصرف روزانه"
          and shop._wallet_label({"type": "purchase", "note": "بیل مصرف روزانه: 0.1"}) == "مصرف روزانه")
    check("a real purchase still reads خرید سرویس",
          shop._wallet_label({"type": "purchase", "note": None}) == "خرید سرویس")

    # ── 6. wallet_summary is retired, not rewritten ──────────────────────
    check("wallet_summary is gone (its two-number job split into the two views)",
          not hasattr(texts, "wallet_summary"))


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All one-vocabulary cases passed.")
