"""Guard (2026-10-26): a customer got «["\u0646\u0627\u0645 ..."]» because ASK_SERVICE_NAME was a
2-element TUPLE (stray comma) and reply_text() JSON-encodes non-str. Every module-level
text constant that is a tuple/list of ONLY strings is a suspect: join it into one string.

Run: python test_texts_are_strings.py   (from shopbot/, no telegram import needed)
"""
import ast
import sys
from pathlib import Path

import texts

tree = ast.parse(Path(__file__).with_name("texts.py").read_text(encoding="utf8"))
bad = [
    f"{t.id} (line {n.lineno})"
    for n in tree.body
    if isinstance(n, ast.Assign) and isinstance(n.value, (ast.Tuple, ast.List))
    and n.value.elts and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in n.value.elts)
    for t in n.targets
]
ok = not bad and isinstance(texts.ASK_SERVICE_NAME, str) and "حذف" in texts.ASK_SERVICE_NAME \
    and "\n" in texts.ASK_SERVICE_NAME
print(("  [OK] " if ok else "[FAIL] ") + "text constants are single strings", bad or "")
sys.exit(0 if ok else 1)
