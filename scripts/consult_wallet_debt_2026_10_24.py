"""[2026-10-24] Council + Seyed (Standard Z): shop-wallet vs ledger-debt unification
for linked reseller customers (owner ask: negative wallet at link? routed books?
hybrid?). Usage: python scripts/consult_wallet_debt_2026_10_24.py
Outputs cached under scripts/consult/."""
import sqlite3, json, urllib.request, concurrent.futures, sys, os, time, shutil, tempfile

sys.stdout.reconfigure(encoding="utf8")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "consult")
os.makedirs(OUT, exist_ok=True)
_tmp = os.path.join(tempfile.gettempdir(), "9router_copy_walletdebt.sqlite")
shutil.copyfile(r"C:\Users\Laptop Markazi\9router\data\db\data.sqlite", _tmp)
con = sqlite3.connect(_tmp)
KEY = con.execute("SELECT key FROM apiKeys WHERE isActive=1 LIMIT 1").fetchone()[0]
con.close()
os.remove(_tmp)
NL = "\n"

CTX = open(os.path.join(OUT, "wallet_debt_context_2026_10_24.txt"), encoding="utf8").read()
QUESTIONS = NL.join(["", "Answer the QUESTIONS listed at the end above."])

# Seyed lens adapted to THIS product (VPN buyers, single-operator trust economy):
# same structure as the Shiraze advisor — founder root-cause, customer-trust-first.
SEYED_SYS = (
    "You are 'Seyed', a product/strategy advisor. Answer strictly through this lens - plain language, "
    "founder root-cause thinking, principle-first. Focus on the CUSTOMER's trust and the OPERATOR's sanity: "
    "a paying customer who is confused about what they owe, or quietly keeps owing without being told, is a "
    "churned customer and an uncollectable debt; a flow that needs the operator to hand-fix books per payment "
    "does not scale. Prefer designs a customer understands without support and the operator can audit in one glance.\n\n"
    "PRODUCT: a one-person VPN reseller shop. Customers buy monthly packages through a Telegram bot "
    "(card-to-card receipt → operator approves → wallet credited → purchase debits wallet). "
    "Long-standing customers were billed manually on a ledger (pay-as-you-go usage); they are now being "
    "linked into the bot, often carrying unpaid ledger debt from their old billing."
)

GENERIC_MSGS = [{"role": "user", "content": CTX + QUESTIONS}]
SEYED_MSGS = [{"role": "system", "content": SEYED_SYS}, {"role": "user", "content": CTX + QUESTIONS}]
SEYED_MODELS = ["ag/gemini-3.8-flash", "ag/gemini-3.8-flash-high", "ag/claude-sonnet-4-6"]
COUNCIL = ["oc/deepseek-v4.1-flash-free", "oc/glm-5.3-flash-free", "oc/mimo-v2.6-flash-free",
           "oc/longcat-2.5-preview-free", "nvidia/nvidia/nemotron-3-ultra-550b-a55b",
           "nvidia/moonshotai/kimi-k3", "ag/gemini-3.8-flash", "nvidia/openai/gpt-oss-20b"]
PREFIX = "walletdebt"


def out_path(kind, model):
    return os.path.join(OUT, PREFIX + "_" + kind + "__" + model.replace("/", "_").replace(":", "_") + ".txt")


def call(job):
    kind, model, msgs = job
    out = out_path(kind, model)
    if os.path.exists(out) and os.path.getsize(out) > 300:
        return kind, model, "cached"
    err = ""
    for attempt in range(3):
        try:
            body = json.dumps({"model": model, "messages": msgs, "temperature": 0.3, "stream": True}).encode()
            req = urllib.request.Request("http://localhost:20128/v1/chat/completions", data=body,
                                         headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
            txt = []
            with urllib.request.urlopen(req, timeout=600) as r:
                for line in r:
                    line = line.decode("utf8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    d = line[5:].strip()
                    if d == "[DONE]":
                        break
                    try:
                        j = json.loads(d)
                    except Exception:
                        continue
                    ch = (j.get("choices") or [{}])[0].get("delta", {}).get("content")
                    if ch:
                        txt.append(ch)
            t = "".join(txt).strip()
            if len(t) > 300:
                open(out, "w", encoding="utf8").write(t)
                return kind, model, "ok %d" % len(t)
            err = "short"
        except Exception as e:
            err = str(e)[:80]
        time.sleep(5)
    return kind, model, "FAILED " + err


jobs = [("council", m, GENERIC_MSGS) for m in COUNCIL] + [("seyed", m, SEYED_MSGS) for m in SEYED_MODELS]
results = []
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
    for r in ex.map(call, jobs):
        print(r[0], r[1], r[2], flush=True)
        results.append(r)
failed = [r for r in results if r[2].startswith("FAILED")]
print("\nDONE. failed:", len(failed))
