"""Three kinds of config (owner 2026-10-27):
  F      client -> Cloudflare -> server          (Unified*, everyone)
  D      client -> middle server -> main server  (6002/6005, «ضریب ۲», everyone)
  TUNNEL client -> Iran server -> US/France      (looks like D to the user)
Only the TUNNEL inbounds are restricted: the operator grants them per account,
so a brand-new account must start WITHOUT them (Marzban's own default, every
inbound, would hand them out). Today the only dedicated tunnel inbound is the
France one (6009); add the US tunnel's tag here when it exists."""
from __future__ import annotations

import os
from typing import Optional

from app.marzban_client import marzban_client

TUNNEL_INBOUND_TAGS = frozenset({
    "VLESS_REALITY_XHTTP_FR_6009",
})


async def default_inbounds_without_tunnels() -> tuple[dict, dict]:
    """(proxies, inbounds) = every live vless inbound except the tunnels.
    Raises (MarzbanUnavailable/...) when Marzban cannot be read - creating the
    account with Marzban's all-inbounds default would leak the tunnels."""
    hosts = await marzban_client.list_inbounds()
    tags = [
        i["tag"] for i in (hosts or {}).get("vless", []) or []
        if i.get("tag") and i["tag"] not in TUNNEL_INBOUND_TAGS
    ]
    if not tags:
        raise ValueError("no non-tunnel vless inbound found on the server")
    return {"vless": {}}, {"vless": tags}


async def apply_no_tunnel_defaults(payload: dict, explicit_inbounds: Optional[dict] = None) -> None:
    """Fill payload proxies/inbounds with the no-tunnel default unless the
    caller chose inbounds explicitly or a config default exists."""
    if explicit_inbounds is not None or payload.get("inbounds"):
        return
    if os.environ.get("TUNNEL_DEFAULT_GUARD", "1") == "0":
        # Off only in the test-suite, whose fake Marzban has no inbound list.
        return
    payload["proxies"], payload["inbounds"] = await default_inbounds_without_tunnels()
