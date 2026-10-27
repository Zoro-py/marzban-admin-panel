"""New accounts start WITHOUT the tunnel inbounds (6002/6005/6009).

Plain `python -m tests.test_tunnel_defaults` from `backend/`.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["TUNNEL_DEFAULT_GUARD"] = "1"
_TMP = Path(tempfile.mkdtemp(prefix="tunnel_test_")) / "t.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.as_posix()}"
os.environ.setdefault("MARZBAN_BASE_URL", "https://panel.test")
os.environ.setdefault("MARZBAN_USERNAME", "t")
os.environ.setdefault("MARZBAN_PASSWORD", "t")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import tunnels  # noqa: E402

failures = []


def check(name, cond, extra=None):
    print(("  [OK] " if cond else "[FAIL] ") + name + (f"  {extra}" if (extra is not None and not cond) else ""))
    if not cond:
        failures.append(name)


LIVE = {"vless": [{"tag": t, "protocol": "vless"} for t in (
    "VLESS_XHTTP_Unified", "VLESS_WS_TLS_2083", "VLESS_REALITY_XHTTP_6002",
    "VLESS_REALITY_TCP_6005", "VLESS_REALITY_XHTTP_FR_6009", "VLESS_REALITY_GRPC_6006")],
    "shadowsocks": [{"tag": "Shadowsocks TCP", "protocol": "shadowsocks"}]}


async def fake_list():
    return LIVE


tunnels.marzban_client.list_inbounds = fake_list


async def main():
    p = {"username": "x"}
    await tunnels.apply_no_tunnel_defaults(p)
    tags = p["inbounds"]["vless"]
    check("1: no tunnel tag in a default create", not (set(tags) & tunnels.TUNNEL_INBOUND_TAGS), tags)
    check("2: normal tags kept", set(tags) == {"VLESS_XHTTP_Unified", "VLESS_WS_TLS_2083", "VLESS_REALITY_GRPC_6006"}, tags)
    check("3: vless only (no shadowsocks)", list(p["inbounds"]) == ["vless"] and p["proxies"] == {"vless": {}})
    p2 = {"username": "y"}
    chosen = {"vless": ["VLESS_REALITY_TCP_6005"]}
    await tunnels.apply_no_tunnel_defaults(p2, chosen)
    check("4: explicit admin choice untouched", "inbounds" not in p2)
    p3 = {"username": "z", "inbounds": {"vless": ["A"]}}
    await tunnels.apply_no_tunnel_defaults(p3)
    check("5: configured default untouched", p3["inbounds"] == {"vless": ["A"]})

    async def boom():
        raise RuntimeError("marzban down")
    tunnels.marzban_client.list_inbounds = boom
    try:
        await tunnels.apply_no_tunnel_defaults({"username": "w"})
        check("6: Marzban down -> refuses (never leaks tunnels)", False)
    except RuntimeError:
        check("6: Marzban down -> refuses (never leaks tunnels)", True)


asyncio.run(main())
print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All tunnel-default cases passed.")
